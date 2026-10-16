"""Qué queda en disco tras procesar un ZIP descargado de la DIAN.

Dos reglas:

1. El ZIP nunca se deja guardado: su PDF y su XML ya están en la base, así que se borra.
2. Las copias sueltas (XML y PDF) solo se borran si S3 las tiene y se comprobó que existen. Con
   solo SharePoint, o sin nube, o con la subida a S3 fallida, esa copia en disco ES el archivo.
"""

from datetime import date, datetime, timezone

import app.infrastructure.persistence.models.concept  # noqa: F401
import app.infrastructure.persistence.models.document_tax  # noqa: F401
import app.infrastructure.persistence.models.issuer  # noqa: F401
import app.infrastructure.persistence.models.receiver  # noqa: F401
import app.infrastructure.persistence.models.tax  # noqa: F401
import pytest
from app.application.use_cases.publish_document_files import PublishDocumentFilesUseCase
from app.infrastructure.persistence.models.document import Document
from app.infrastructure.persistence.repositories.document_repository import DocumentRepository
from app.infrastructure.queue.download_queue import _files_stored_in_db, _finalize_local_files
from app.infrastructure.storage.s3_storage_adapter import S3StorageAdapter
from app.infrastructure.storage.sharepoint_storage_adapter import SharePointStorageAdapter

PDF = b"%PDF-1.4\ncontenido\n%%EOF\n"
XML = b"<Invoice><ID>FBC98359</ID></Invoice>"


# ── Qué se borra del disco ─────────────────────────────────────────────────


@pytest.fixture
def disco(tmp_path):
    descargas = tmp_path / "downloads"
    procesados = descargas / "processed"
    procesados.mkdir(parents=True)
    zip_path = descargas / "doc.zip"
    zip_path.write_bytes(b"PK\x03\x04")
    xml = procesados / "doc.xml"
    pdf = procesados / "doc.pdf"
    xml.write_bytes(XML)
    pdf.write_bytes(PDF)
    return zip_path, procesados, (pdf, xml)


class TestElZipNoSeConserva:
    def test_con_el_contenido_en_la_base_el_zip_se_borra(self, disco):
        zip_path, procesados, copias = disco

        _finalize_local_files(zip_path, procesados, zip_deletable=True, extracted_paths=copias)

        assert not zip_path.exists()
        assert not (procesados / "doc.zip").exists()  # tampoco se archiva en processed/

    def test_si_no_se_pudo_confirmar_el_contenido_el_zip_se_archiva(self, disco):
        zip_path, procesados, copias = disco

        _finalize_local_files(zip_path, procesados, zip_deletable=False, extracted_paths=copias)

        assert not zip_path.exists()
        assert (procesados / "doc.zip").exists()  # podría ser la única copia


class TestLasCopiasSueltas:
    def test_con_s3_verificado_se_borran(self, disco):
        zip_path, procesados, copias = disco

        _finalize_local_files(
            zip_path, procesados, zip_deletable=True, s3_verified=True, extracted_paths=copias
        )

        assert not any(c.exists() for c in copias)

    def test_sin_verificar_s3_se_conservan(self, disco):
        zip_path, procesados, copias = disco

        _finalize_local_files(
            zip_path, procesados, zip_deletable=True, s3_verified=False, extracted_paths=copias
        )

        assert all(c.exists() for c in copias)
        assert not zip_path.exists()  # el ZIP se va igual

    def test_con_solo_sharepoint_el_archivo_se_queda_en_local(self, disco):
        """SharePoint-only llega aquí con s3_verified=False: no cuenta como respaldo."""
        zip_path, procesados, copias = disco

        _finalize_local_files(
            zip_path, procesados, zip_deletable=True, s3_verified=False, extracted_paths=copias
        )

        assert all(c.exists() for c in copias)


# ── Contenido en base de datos ─────────────────────────────────────────────


def _documento(db_session, numero, **kwargs) -> Document:
    base = {
        "document_name": f"{numero}.xml",
        "document_number": numero,
        "date": date(2026, 4, 29),
        "hour": "10:00",
        "currency": "COP",
        "document_type": "Factura de venta",
        "uuid": numero,
        "issuer_name": "BODEGA Y COCINA SAS",
        "issuer_nit": "830044885",
        "receiver_name": "MI EMPRESA",
        "receiver_nit": "800987654",
        "subtotal": 1.0,
        "total_taxes": 0.0,
        "retefuente": 0.0,
        "reteica": 0.0,
        "total": 1.0,
        "register_at": datetime.now(timezone.utc),
        "status": 100,
    }
    base.update(kwargs)
    doc = Document(**base)
    db_session.add(doc)
    db_session.commit()
    db_session.refresh(doc)
    return doc


class TestContenidoEnLaBase:
    def test_con_xml_guardado_se_reconoce(self, db_session):
        doc = _documento(db_session, "A1", xml_data=XML)

        assert _files_stored_in_db(db_session, doc.id) is True

    def test_sin_xml_no_se_puede_borrar_el_zip(self, db_session):
        doc = _documento(db_session, "A2", xml_data=None)

        assert _files_stored_in_db(db_session, doc.id) is False

    def test_documento_inexistente(self, db_session):
        assert _files_stored_in_db(db_session, 999999) is False


# ── Verificación en S3 ─────────────────────────────────────────────────────


class _ClienteBoto:
    def __init__(self, tamano=None, falla=False):
        self._tamano = tamano
        self._falla = falla
        self.pedidos = []

    def head_object(self, Bucket, Key):
        self.pedidos.append((Bucket, Key))
        if self._falla:
            raise RuntimeError("404 Not Found")
        return {"ContentLength": self._tamano}


def _s3(cliente):
    adaptador = S3StorageAdapter("bucket", "us-east-1", "k", "s", key_prefix="ikbo")
    adaptador._client = cliente
    return adaptador


class TestVerificarEnS3:
    @pytest.mark.asyncio
    async def test_existe_con_el_tamano_esperado(self):
        cliente = _ClienteBoto(tamano=len(XML))

        assert await _s3(cliente).verify("s3://bucket/ikbo/a.xml", len(XML)) is True
        assert cliente.pedidos == [("bucket", "ikbo/a.xml")]

    @pytest.mark.asyncio
    async def test_tamano_distinto_no_se_acepta(self):
        assert await _s3(_ClienteBoto(tamano=3)).verify("s3://bucket/ikbo/a.xml", len(XML)) is False

    @pytest.mark.asyncio
    async def test_objeto_inexistente_no_se_acepta(self):
        assert await _s3(_ClienteBoto(falla=True)).verify("s3://bucket/ikbo/a.xml", 5) is False

    @pytest.mark.asyncio
    async def test_ubicacion_de_otro_bucket_no_se_acepta(self):
        cliente = _ClienteBoto(tamano=len(XML))

        assert await _s3(cliente).verify("s3://otro/ikbo/a.xml", len(XML)) is False
        assert cliente.pedidos == []  # ni siquiera consulta


class _Config:
    def __init__(self, settings):
        self._settings = settings

    async def get_storage_settings(self, tenant_slug):
        return self._settings


_S3 = {"bucket_name": "b", "region": "us-east-1", "access_key_id": "k", "secret_access_key": "s"}
_SP = {
    "azure_tenant_id": "t",
    "client_id": "c",
    "client_secret": "s",
    "site_id": "site",
    "drive_id": "drive",
}


@pytest.fixture
def nube(monkeypatch):
    """Sube sin red y deja que cada prueba decida qué responde la verificación en S3."""
    estado = {"verifica": True, "verificados": []}

    async def _guardar_s3(self, content, filename):
        return f"s3://{self._bucket_name}/{self._build_key(filename)}"

    async def _verificar(self, location, expected_size):
        estado["verificados"].append((location, expected_size))
        return estado["verifica"]

    async def _guardar_sp(self, content, filename):
        return f"https://sp.example.com/{filename}"

    monkeypatch.setattr(S3StorageAdapter, "save", _guardar_s3)
    monkeypatch.setattr(S3StorageAdapter, "verify", _verificar)
    monkeypatch.setattr(SharePointStorageAdapter, "save", _guardar_sp)
    return estado


async def _publicar(db_session, settings, **kwargs):
    doc = _documento(db_session, kwargs.pop("numero", "P1"), **kwargs)
    uso = PublishDocumentFilesUseCase(DocumentRepository(db_session), _Config(settings))
    return doc, await uso.execute(doc.id, tenant_slug="ikbo")


class TestPublicacionInformaSiS3EstaVerificado:
    @pytest.mark.asyncio
    async def test_con_s3_y_objetos_verificados(self, db_session, nube):
        doc, r = await _publicar(db_session, {"s3": _S3}, pdf_data=PDF, xml_data=XML)

        assert r["s3_verified"] is True
        assert r["pdf_url"].startswith("s3://") and r["xml_url"].startswith("s3://")
        assert sorted(tam for _, tam in nube["verificados"]) == sorted([len(PDF), len(XML)])

    @pytest.mark.asyncio
    async def test_si_un_objeto_no_se_puede_verificar_no_hay_permiso_de_borrar(
        self, db_session, nube
    ):
        nube["verifica"] = False

        _, r = await _publicar(db_session, {"s3": _S3}, numero="P2", pdf_data=PDF, xml_data=XML)

        assert r["s3_verified"] is False

    @pytest.mark.asyncio
    async def test_solo_sharepoint_nunca_cuenta_como_verificado(self, db_session, nube):
        _, r = await _publicar(db_session, {"sharepoint": _SP}, numero="P3", pdf_data=PDF, xml_data=XML)

        assert r["s3_verified"] is False
        assert r["pdf_url"] is None  # el enlace sigue siendo solo el de S3
        assert nube["verificados"] == []

    @pytest.mark.asyncio
    async def test_sin_nube_el_respaldo_es_local_y_no_hay_verificacion(
        self, db_session, nube, tmp_path, monkeypatch
    ):
        monkeypatch.setenv("DOWNLOADS_DIR", str(tmp_path))

        _, r = await _publicar(db_session, None, numero="P4", pdf_data=PDF, xml_data=XML)

        assert r["s3_verified"] is False

    @pytest.mark.asyncio
    async def test_con_s3_y_sharepoint_el_enlace_es_el_de_s3(self, db_session, nube):
        _, r = await _publicar(
            db_session, {"s3": _S3, "sharepoint": _SP}, numero="P5", pdf_data=PDF, xml_data=XML
        )

        assert r["pdf_url"].startswith("s3://")
        assert r["s3_verified"] is True

    @pytest.mark.asyncio
    async def test_si_s3_falla_y_sharepoint_funciona_la_copia_local_se_conserva(
        self, db_session, nube, monkeypatch
    ):
        async def _s3_caido(self, content, filename):
            raise RuntimeError("S3 caído")

        monkeypatch.setattr(S3StorageAdapter, "save", _s3_caido)

        _, r = await _publicar(
            db_session, {"s3": _S3, "sharepoint": _SP}, numero="P6", pdf_data=PDF, xml_data=XML
        )

        assert r["s3_verified"] is False
        assert r["warnings"]

    @pytest.mark.asyncio
    async def test_un_documento_ya_publicado_se_verifica_contra_lo_guardado(self, db_session, nube):
        """Sin volver a subir (el enlace ya existía), la verificación sigue siendo posible."""
        doc = _documento(
            db_session,
            "P7",
            pdf_data=PDF,
            xml_data=XML,
            pdf_url="s3://b/ikbo/p.pdf",
            xml_url="s3://b/ikbo/p.xml",
            pdf_storage_locations={"S3StorageAdapter": "s3://b/ikbo/p.pdf"},
            xml_storage_locations={"S3StorageAdapter": "s3://b/ikbo/p.xml"},
        )
        uso = PublishDocumentFilesUseCase(DocumentRepository(db_session), _Config({"s3": _S3}))

        r = await uso.execute(doc.id, tenant_slug="ikbo")

        assert r["uploaded"] == [] and r["s3_verified"] is True

    @pytest.mark.asyncio
    async def test_un_documento_solo_con_xml_se_verifica_solo_el_xml(self, db_session, nube):
        _, r = await _publicar(db_session, {"s3": _S3}, numero="P8", pdf_data=None, xml_data=XML)

        assert r["s3_verified"] is True
        assert [tam for _, tam in nube["verificados"]] == [len(XML)]
