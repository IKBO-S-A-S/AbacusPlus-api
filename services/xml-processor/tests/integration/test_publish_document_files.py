"""
RF-03 — Publicación del PDF y el XML en los backends de almacenamiento del tenant.

El alcance pide subir los archivos a S3 y guardar el enlace que retorna, para renderizarlo
luego en el detalle. Las credenciales de S3 son siempre las que el tenant configuró en
integration-config-service — nunca variables de entorno globales del servicio. Estas
pruebas fijan cuatro garantías:

- **Las credenciales vienen del tenant.** `resolve_storage` consulta la configuración del
  tenant; sin ninguna configurada, el respaldo es el disco local, nunca un S3 global.
- **Idempotencia.** Republicar un archivo con enlace ya guardado no lo vuelve a subir.
- **Independencia entre archivos y entre backends.** Que falle un backend no le cuesta el
  enlace al otro archivo.
- **Nada se pierde ante un fallo.** La subida es best-effort: el documento ya está
  guardado y una caída del backend no puede propagarse ni dejar datos a medias.
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
from app.infrastructure.storage.s3_storage_adapter import S3StorageAdapter

PDF = b"%PDF-1.4\ncontenido\n%%EOF\n"
XML = b"<Invoice><ID>FBC98359</ID></Invoice>"


class _FakeIntegrationConfigClient:
    """Doble de integration-config-service: retorna la configuración de almacenamiento
    que el tenant guardó (o `None`, como si no hubiera configurado ninguna)."""

    def __init__(self, settings=None):
        self._settings = settings

    async def get_storage_settings(self, tenant_slug):
        return self._settings


def _tenant_s3_settings(naming_pattern: str = "{numero_documento}"):
    """Credenciales de S3 tal como las guardó ESTE tenant — nunca del .env del servicio."""
    return {
        "s3": {
            "bucket_name": "bucket-del-tenant",
            "region": "us-east-1",
            "access_key_id": "AKIA-tenant",
            "secret_access_key": "secret-tenant",
        },
        "naming_pattern": naming_pattern,
    }


@pytest.fixture(autouse=True)
def fake_s3_uploads(monkeypatch):
    """Evita llamadas reales a S3: registra la clave en vez de emitir la subida."""
    calls: list[tuple[str, str]] = []

    async def _save(self, content, filename):
        key = self._build_key(filename)
        calls.append((filename, key))
        return f"s3://{self._bucket_name}/{key}"

    monkeypatch.setattr(S3StorageAdapter, "save", _save)
    return calls


def _make_doc(db_session, **kwargs) -> Document:
    defaults = {
        "document_name": "test.xml",
        "document_number": "FBC98359",
        "date": date(2026, 4, 29),
        "hour": "10:00",
        "currency": "COP",
        "document_type": "Factura de venta",
        "uuid": "rf03-1",
        "issuer_name": "BODEGA Y COCINA SAS",
        "issuer_nit": "830044885",
        "receiver_name": "MI EMPRESA",
        "receiver_nit": "800987654",
        "subtotal": 148600.0,
        "total_taxes": 28234.0,
        "retefuente": 0.0,
        "reteica": 0.0,
        "total": 176834.0,
        "register_at": datetime.now(timezone.utc),
        "status": 200,
        "pdf_data": PDF,
        "xml_data": XML,
    }
    defaults.update(kwargs)
    doc = Document(**defaults)
    db_session.add(doc)
    db_session.commit()
    db_session.refresh(doc)
    return doc


@pytest.fixture
def repo(db_session):
    return DocumentRepository(db_session)


class TestPublication:
    @pytest.mark.asyncio
    async def test_publishes_both_files_to_the_tenant_s3_and_persists_the_links(
        self, db_session, repo, fake_s3_uploads
    ):
        doc = _make_doc(db_session)
        client = _FakeIntegrationConfigClient(_tenant_s3_settings())

        result = await PublishDocumentFilesUseCase(repo, client).execute(doc.id, "ikbo")

        assert result["uploaded"] == ["pdf", "xml"]
        db_session.refresh(doc)
        assert doc.pdf_url == "s3://bucket-del-tenant/FBC98359.pdf"
        assert doc.xml_url == "s3://bucket-del-tenant/FBC98359.xml"

    @pytest.mark.asyncio
    async def test_the_filename_comes_from_the_document_number(
        self, db_session, repo, fake_s3_uploads
    ):
        doc = _make_doc(db_session)
        client = _FakeIntegrationConfigClient(_tenant_s3_settings())

        await PublishDocumentFilesUseCase(repo, client).execute(doc.id, "ikbo")

        filenames = [f for f, _ in fake_s3_uploads]
        assert "FBC98359.pdf" in filenames
        assert "FBC98359.xml" in filenames

    @pytest.mark.asyncio
    async def test_a_document_number_with_odd_characters_is_sanitised(
        self, db_session, repo, fake_s3_uploads
    ):
        """El nombre viaja en la clave del objeto: no puede llevar separadores de ruta
        (los espacios sí se conservan — el patrón de nombre es legible a propósito)."""
        doc = _make_doc(db_session, document_number="FB/C 98\\359", uuid="rf03-raro")
        client = _FakeIntegrationConfigClient(_tenant_s3_settings())

        await PublishDocumentFilesUseCase(repo, client).execute(doc.id, "ikbo")

        nombre = next(f for f, _ in fake_s3_uploads if f.endswith(".pdf"))
        assert "/" not in nombre and "\\" not in nombre

    @pytest.mark.asyncio
    async def test_without_an_explicit_naming_pattern_the_tenant_default_applies(
        self, db_session, repo, fake_s3_uploads
    ):
        """Sin `naming_pattern` en la config del tenant, se usa el patrón por defecto
        (fecha, tipo, número, emisor) — el mismo que aplicaría el resto de la plataforma."""
        doc = _make_doc(db_session)
        client = _FakeIntegrationConfigClient({"s3": _tenant_s3_settings()["s3"]})

        await PublishDocumentFilesUseCase(repo, client).execute(doc.id, "ikbo")

        filenames = [f for f, _ in fake_s3_uploads]
        esperado = "IKB - DOCU - 20260429 - V01 - Factura de venta FBC98359 BODEGA Y COCINA SAS"
        assert f"{esperado}.pdf" in filenames
        assert f"{esperado}.xml" in filenames

    @pytest.mark.asyncio
    async def test_a_missing_document_is_rejected(self, repo):
        client = _FakeIntegrationConfigClient(_tenant_s3_settings())
        with pytest.raises(ValueError):
            await PublishDocumentFilesUseCase(repo, client).execute(999999, "ikbo")


class TestIdempotency:
    @pytest.mark.asyncio
    async def test_a_file_with_a_link_is_not_uploaded_again(
        self, db_session, repo, fake_s3_uploads
    ):
        doc = _make_doc(db_session, pdf_url="https://s3.example/ya-existe")
        client = _FakeIntegrationConfigClient(_tenant_s3_settings())

        result = await PublishDocumentFilesUseCase(repo, client).execute(doc.id, "ikbo")

        assert "pdf" in result["skipped"]
        assert not any(f.endswith(".pdf") for f, _ in fake_s3_uploads)
        db_session.refresh(doc)
        assert doc.pdf_url == "https://s3.example/ya-existe"

    @pytest.mark.asyncio
    async def test_overwrite_republishes_a_broken_link(self, db_session, repo, fake_s3_uploads):
        doc = _make_doc(db_session, pdf_url="https://s3.example/vencido")
        client = _FakeIntegrationConfigClient(_tenant_s3_settings())

        await PublishDocumentFilesUseCase(repo, client).execute(doc.id, "ikbo", overwrite=True)

        db_session.refresh(doc)
        assert doc.pdf_url == "s3://bucket-del-tenant/FBC98359.pdf"

    @pytest.mark.asyncio
    async def test_running_twice_uploads_only_once(self, db_session, repo, fake_s3_uploads):
        doc = _make_doc(db_session)
        client = _FakeIntegrationConfigClient(_tenant_s3_settings())
        uc = PublishDocumentFilesUseCase(repo, client)

        await uc.execute(doc.id, "ikbo")
        segunda = await uc.execute(doc.id, "ikbo")

        assert segunda["uploaded"] == []
        assert len(fake_s3_uploads) == 2  # pdf y xml, de la primera ejecución


class TestPartialAndFailure:
    @pytest.mark.asyncio
    async def test_a_document_without_xml_only_publishes_the_pdf(
        self, db_session, repo, fake_s3_uploads
    ):
        doc = _make_doc(db_session, xml_data=None)
        client = _FakeIntegrationConfigClient(_tenant_s3_settings())

        result = await PublishDocumentFilesUseCase(repo, client).execute(doc.id, "ikbo")

        assert result["uploaded"] == ["pdf"]
        assert "xml" in result["skipped"]

    @pytest.mark.asyncio
    async def test_a_failing_backend_does_not_cost_the_other_file(
        self, db_session, repo, monkeypatch
    ):
        """Que falle la subida del XML no puede costar el enlace del PDF."""
        doc = _make_doc(db_session)
        client = _FakeIntegrationConfigClient(_tenant_s3_settings())

        async def _flaky_save(self, content, filename):
            if filename.endswith(".xml"):
                raise RuntimeError("boto3 timeout")
            return f"s3://bucket-del-tenant/{filename}"

        monkeypatch.setattr(S3StorageAdapter, "save", _flaky_save)

        result = await PublishDocumentFilesUseCase(repo, client).execute(doc.id, "ikbo")

        assert result["uploaded"] == ["pdf"]
        assert result["warnings"]
        db_session.refresh(doc)
        assert doc.pdf_url == "s3://bucket-del-tenant/FBC98359.pdf"

    @pytest.mark.asyncio
    async def test_without_tenant_configuration_falls_back_to_local_storage(
        self, db_session, repo, tmp_path, monkeypatch
    ):
        """Sin S3/SharePoint configurados por el tenant, el respaldo es el disco local —
        nunca variables de entorno globales del servicio. El respaldo local no alimenta
        pdf_url/xml_url: solo un backend S3 configurado por el tenant lo hace."""
        doc = _make_doc(db_session)
        client = _FakeIntegrationConfigClient(settings=None)
        monkeypatch.setenv("DOWNLOADS_DIR", str(tmp_path))

        result = await PublishDocumentFilesUseCase(repo, client).execute(doc.id, "ikbo")

        assert result["uploaded"] == ["pdf", "xml"]
        esperado = "IKB - DOCU - 20260429 - V01 - Factura de venta FBC98359 BODEGA Y COCINA SAS"
        assert (tmp_path / "processed" / "pdf" / f"{esperado}.pdf").exists()
        db_session.refresh(doc)
        assert doc.pdf_url is None
