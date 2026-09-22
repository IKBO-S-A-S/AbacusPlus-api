import asyncio
import contextlib
import logging
import os
import shutil
import zipfile
from pathlib import Path, PurePosixPath
from typing import Optional

import httpx

from app.application.use_cases.process_xml import ProcessXmlUseCase
from app.application.use_cases.publish_document_files import PublishDocumentFilesUseCase
from app.domain.exceptions.base import DuplicateEntityException
from app.domain.services.document_naming import sanitize_name
from app.infrastructure.clients.integration_config_client import IntegrationConfigClient
from app.infrastructure.clients.rag_client import RagClient
from app.infrastructure.config.database import SessionLocal
from app.infrastructure.config.tenant_connection_manager import get_session_for_tenant
from app.infrastructure.persistence.models.processing_log import ProcessingLog
from app.infrastructure.persistence.repositories.concept_repository import ConceptRepository
from app.infrastructure.persistence.repositories.document_repository import DocumentRepository
from app.infrastructure.persistence.repositories.issuer_repository import IssuerRepository
from app.infrastructure.persistence.repositories.processing_log_repository import (
    ProcessingLogRepository,
)
from app.infrastructure.persistence.repositories.receiver_repository import ReceiverRepository
from app.infrastructure.persistence.repositories.tax_repository import TaxRepository
from app.infrastructure.queue.job_progress_store import JobProgressStore

logger = logging.getLogger(__name__)

# La cola transporta tuplas (Path, job_id | None).
# job_id es None cuando el item fue encolado por process-downloads (modo legacy).
_queue: asyncio.Queue = asyncio.Queue()

_progress_store: Optional[JobProgressStore] = None


def get_queue() -> asyncio.Queue:
    return _queue


def get_progress_store() -> JobProgressStore:
    global _progress_store
    if _progress_store is None:
        _progress_store = JobProgressStore(redis_url=os.getenv("REDIS_URL", "redis://redis:6379"))
    return _progress_store


def _peek_xml_filename(file_path: Path) -> Optional[str]:
    """Lee solo la tabla del ZIP para obtener el nombre del primer XML, sin descomprimir."""
    with contextlib.suppress(Exception), zipfile.ZipFile(str(file_path)) as zf:
        xml_files = [
            n
            for n in zf.namelist()
            if n.lower().endswith(".xml") and not n.startswith(("__MACOSX/", "._"))
        ]
        if xml_files:
            return PurePosixPath(xml_files[0]).name
    return None


def _build_pdf_name(
    document_date, document_type: str, document_number: str, issuer_name: str
) -> str:
    """
    Construye el nombre del PDF con la nomenclatura:
    IKB - DOCU - AAAAMMDD - V01 - {tipo_documento} {numero_documento} {tercero}
    """
    try:
        date_str = document_date.strftime("%Y%m%d")
    except Exception:
        date_str = str(document_date).replace("-", "")[:8]

    doc_type = sanitize_name(document_type or "")
    doc_number = sanitize_name(document_number or "")
    issuer = sanitize_name(issuer_name or "")
    suffix = " ".join(part for part in [doc_type, doc_number, issuer] if part).strip()
    return f"IKB - DOCU - {date_str} - V01 - {suffix}.pdf"


def _extract_files_to_processed(
    file_path: Path, processed_dir: Path, document_data: dict
) -> tuple[Optional[bytes], Optional[bytes], Optional[Path], Optional[Path]]:
    """
    Descomprime el ZIP y:
    - Deja el XML en processed/xml/{nombre_original}.xml
    - Deja el PDF en processed/pdf/{nomenclatura_IKB}.pdf

    Retorna (pdf_bytes, xml_bytes, pdf_path, xml_path). Los bytes alimentan la base de datos
    para poder visualizar el documento sin volver a la DIAN; las rutas son las que
    `_finalize_local_files` borra una vez confirmada la subida a S3/SharePoint — sin esta
    copia intermedia en disco, un tenant con almacenamiento en la nube acumularía para
    siempre el mismo PDF/XML dos veces: uno en el bucket/SharePoint y otro aquí.
    """
    pdf_dir = processed_dir / "pdf"
    xml_dir = processed_dir / "xml"
    pdf_dir.mkdir(exist_ok=True)
    xml_dir.mkdir(exist_ok=True)

    pdf_bytes: Optional[bytes] = None
    xml_bytes: Optional[bytes] = None
    pdf_path: Optional[Path] = None
    xml_path: Optional[Path] = None
    try:
        with zipfile.ZipFile(str(file_path)) as zf:
            for member in zf.namelist():
                if member.startswith(("__MACOSX/", "._")):
                    continue
                name_lower = member.lower()
                member_name = PurePosixPath(member).name

                if name_lower.endswith(".xml"):
                    data = zf.read(member)
                    if xml_bytes is None:
                        xml_bytes = data
                    dest = xml_dir / member_name
                    dest.write_bytes(data)
                    xml_path = dest
                    logger.info("XML extraído → %s", dest)

                elif name_lower.endswith(".pdf"):
                    data = zf.read(member)
                    # Solo aceptamos un PDF real (firma %PDF-) para no guardar basura.
                    if data[:5] == b"%PDF-":
                        pdf_bytes = data
                    pdf_name = _build_pdf_name(
                        document_data.get("date"),
                        document_data.get("document_type", ""),
                        document_data.get("document_number", ""),
                        document_data.get("issuer_name", ""),
                    )
                    dest = pdf_dir / pdf_name
                    dest.write_bytes(data)
                    pdf_path = dest
                    logger.info("PDF extraído y renombrado → %s", dest)

    except Exception as e:
        logger.warning("No se pudieron extraer archivos del ZIP %s: %s", file_path.name, e)

    return pdf_bytes, xml_bytes, pdf_path, xml_path


def _is_cloud_backed(locations: dict) -> bool:
    """True si el mapa de ubicaciones tiene algún backend que no sea el fallback local.

    `resolve_storage` (RF-03) resuelve los backends de un tenant una sola vez para PDF y XML
    por igual: si hay S3 y/o SharePoint configurados y activos se usan esos —nunca junto con
    local—, y si no hay ninguno cae a `LocalStorageAdapter`. Por eso basta con mirar las
    claves de un mapa para saber si el documento quedó respaldado en la nube.
    """
    return bool(locations) and any(k != "LocalStorageAdapter" for k in locations)


def _finalize_local_files(
    file_path: Path,
    processed_dir: Path,
    cloud_backed: bool,
    extracted_paths: tuple[Optional[Path], ...] = (),
) -> None:
    """Decide qué hacer con el ZIP y las copias locales extraídas de él.

    Si el tenant no tiene S3 ni SharePoint configurados, el disco local ES el almacenamiento
    definitivo del documento — se conserva todo, como hasta ahora. Si sí los tiene y la
    publicación no reportó ningún warning, esas copias en disco son redundantes desde el
    instante en que S3/SharePoint confirmaron la subida: guardarlas para siempre solo
    acumula espacio sin ningún documento que no esté ya seguro en la nube.
    """
    if not cloud_backed:
        shutil.move(str(file_path), str(processed_dir / file_path.name))
        return

    for path in extracted_paths:
        if path is not None:
            with contextlib.suppress(Exception):
                path.unlink()
    with contextlib.suppress(Exception):
        file_path.unlink()
    logger.info(
        "ZIP y copias locales borrados tras confirmar subida a S3/SharePoint: %s", file_path.name
    )


def _read_pdf_from_zip(file_path: Path) -> Optional[bytes]:
    """Lee (sin escribir a disco) el primer PDF válido dentro del ZIP. None si no hay."""
    try:
        with zipfile.ZipFile(str(file_path)) as zf:
            for member in zf.namelist():
                if member.startswith(("__MACOSX/", "._")):
                    continue
                if member.lower().endswith(".pdf"):
                    data = zf.read(member)
                    if data[:5] == b"%PDF-":
                        return data
    except Exception as e:
        logger.warning("No se pudo leer PDF del ZIP %s: %s", file_path.name, e)
    return None


def _read_xml_from_zip(file_path: Path) -> Optional[bytes]:
    """Lee (sin escribir a disco) el primer XML dentro del ZIP. None si no hay."""
    try:
        with zipfile.ZipFile(str(file_path)) as zf:
            for member in zf.namelist():
                if member.startswith(("__MACOSX/", "._")):
                    continue
                if member.lower().endswith(".xml"):
                    return zf.read(member)
    except Exception as e:
        logger.warning("No se pudo leer XML del ZIP %s: %s", file_path.name, e)
    return None


class FileWrapper:
    """Envuelve un Path como UploadFile-compatible para ProcessXmlUseCase."""

    def __init__(self, path: Path):
        self.filename = path.name
        self._path = path

    async def read(self) -> bytes:
        return self._path.read_bytes()


async def process_queue_worker() -> None:
    """Worker asyncio que consume la cola y procesa cada ZIP."""
    logger.info("Worker de procesamiento de ZIPs iniciado")
    while True:
        item = await _queue.get()
        # Soporta tanto tuplas (path, job_id) como paths sueltos (compatibilidad)
        if isinstance(item, tuple):
            if len(item) == 3:
                file_path, job_id, tenant_slug = item
            else:
                file_path, job_id = item
                tenant_slug = ""
        else:
            file_path, job_id, tenant_slug = item, None, ""

        try:
            await _process_single_file(file_path, job_id, tenant_slug)
        except Exception as e:
            logger.error("Error inesperado procesando %s: %s", file_path.name, e)
        finally:
            _queue.task_done()


def build_integration_config_client(tenant_slug: str) -> IntegrationConfigClient:
    """Cliente del catálogo para el procesamiento en segundo plano.

    Existe por el mismo motivo que `build_siigo_service_client`: centralizar la URL del
    servicio y dejar claro que aquí se habla por el canal interno. La descarga masiva no
    tiene JWT de usuario, así que la ruta con token responde 403 — y ese 403, silenciado por
    el `except` del cliente, es lo que dejó 151 de 152 líneas sin `tax_id`.
    """
    url = os.getenv("INTEGRATION_CONFIG_URL", "http://integration-config-service:8007")
    return IntegrationConfigClient(base_url=url, tenant_slug=tenant_slug)


async def _process_single_file(
    file_path: Path, job_id: Optional[str], tenant_slug: str = ""
) -> None:
    downloads_dir = file_path.parent
    processed_dir = downloads_dir / "processed"
    errors_dir = downloads_dir / "errors"
    processed_dir.mkdir(exist_ok=True)
    errors_dir.mkdir(exist_ok=True)

    progress = get_progress_store() if job_id else None
    xml_filename = _peek_xml_filename(file_path)

    db = get_session_for_tenant(tenant_slug) if tenant_slug else SessionLocal()
    try:
        use_case = ProcessXmlUseCase(
            document_repo=DocumentRepository(db),
            issuer_repo=IssuerRepository(db),
            receiver_repo=ReceiverRepository(db),
            tax_repo=TaxRepository(db),
            concept_repo=ConceptRepository(db),
            # RF-08: la descarga desde la DIAN no genera conocimiento. Un documento recién
            # bajado no tiene ninguna decisión contable validada; el RAG se alimenta cuando
            # ese documento llegue a contabilizarse en SIIGO.
            tenant_slug=tenant_slug,
            # El catálogo de impuestos, por el canal interno. Sin él, cada línea del
            # documento queda sin `tax_id`: la interfaz no muestra su impuesto y el envío
            # a SIIGO no puede respetar el que el contador hubiera elegido.
            integration_config_client=(
                build_integration_config_client(tenant_slug) if tenant_slug else None
            ),
        )
        log_repo = ProcessingLogRepository(db)

        result = await use_case.execute(FileWrapper(file_path))

        pdf_bytes, xml_bytes, pdf_path, xml_path = _extract_files_to_processed(
            file_path, processed_dir, result["data"]
        )

        # Best-effort: un backend caído no puede tumbar el procesamiento del XML, pero el
        # fallo debe quedar en processing_logs (consultable por API) y no solo en el log de
        # texto del contenedor — de lo contrario una caída de S3/SharePoint pasa inadvertida.
        storage_status: Optional[str] = None
        storage_error: Optional[str] = None
        cloud_backed = False

        # Almacena el PDF y el XML oficiales de la DIAN (venían dentro del ZIP) ligados al
        # documento, para poder visualizarlos luego sin volver a la DIAN. Best-effort: si
        # falla, no rompe el procesamiento del documento.
        if pdf_bytes or xml_bytes:
            try:
                repo = DocumentRepository(db)
                doc = repo.get_by_id(result["document_id"])
                if doc is not None:
                    if pdf_bytes:
                        doc.pdf_data = pdf_bytes
                        doc.pdf_source = "dian_official"
                    if xml_bytes:
                        doc.xml_data = xml_bytes
                    db.commit()

                    # RF-03: la publicación en S3/SharePoint vive en su propio caso de uso, que
                    # lee los bytes ya almacenados. Así la misma lógica sirve a esta ruta, a la
                    # carga manual y al reintento posterior, sin duplicarse en tres sitios.
                    publicacion = await PublishDocumentFilesUseCase(repo).execute(
                        result["document_id"], tenant_slug=tenant_slug
                    )
                    if publicacion["warnings"]:
                        storage_status = "error"
                        storage_error = "; ".join(publicacion["warnings"])
                    elif publicacion["uploaded"]:
                        storage_status = "ok"
                        # Solo se borra la copia local si CADA backend configurado aceptó la
                        # subida (sin warnings) — con uno solo caído, la copia en disco sigue
                        # siendo la única evidencia de ese archivo hasta el próximo reintento.
                        refreshed = repo.get_by_id(result["document_id"])
                        if refreshed is not None:
                            cloud_backed = _is_cloud_backed(
                                refreshed.pdf_storage_locations
                            ) or _is_cloud_backed(refreshed.xml_storage_locations)
                    logger.info(
                        "Documento DIAN almacenado (id=%s, publicado en: %s)",
                        result["document_id"],
                        ", ".join(publicacion["uploaded"]) or "nada",
                    )
            except Exception as e:  # best-effort: no romper el procesamiento del XML
                db.rollback()
                storage_status = "error"
                storage_error = f"{type(e).__name__}: {e}"
                logger.warning("No se pudo almacenar PDF/XML oficial en BD: %s", e)

        acc_status, acc_error = await _trigger_accounting(result["document_id"])

        log_repo.create(
            ProcessingLog(
                filename=file_path.name,
                xml_filename=xml_filename or result.get("filename"),
                status="added",
                document_id=result["document_id"],
                document_number=result["data"]["document_number"],
                accounting_status=acc_status,
                accounting_error=acc_error,
                storage_status=storage_status,
                storage_error=storage_error,
            )
        )
        _finalize_local_files(file_path, processed_dir, cloud_backed, (pdf_path, xml_path))
        if not cloud_backed:
            logger.info("ZIP procesado → XML: %s — movido a processed/", xml_filename)

        if progress:
            await progress.mark_xml_done(job_id, "added", document_id=result["document_id"])
            await progress.mark_accounting_done(job_id, acc_status or "error", error=acc_error)

    except DuplicateEntityException as e:
        log_repo = ProcessingLogRepository(db)
        doc_number = e.message.split(": ")[-1] if ": " in e.message else ""

        existing_doc = DocumentRepository(db).get_by_document_number(doc_number)
        acc_status, acc_error = None, None
        storage_status: Optional[str] = None
        storage_error: Optional[str] = None
        cloud_backed = False
        if existing_doc:
            # Backfill: si el documento ya existía pero aún no tenía el PDF/XML oficial,
            # los tomamos del ZIP (best-effort) y los almacenamos.
            try:
                changed = False
                if not getattr(existing_doc, "pdf_data", None):
                    pdf_bytes = _read_pdf_from_zip(file_path)
                    if pdf_bytes:
                        existing_doc.pdf_data = pdf_bytes
                        existing_doc.pdf_source = "dian_official"
                        changed = True
                if not getattr(existing_doc, "xml_data", None):
                    xml_bytes = _read_xml_from_zip(file_path)
                    if xml_bytes:
                        existing_doc.xml_data = xml_bytes
                        changed = True
                if changed:
                    db.commit()
                    # Los enlaces se publican desde los bytes recién guardados; el caso de
                    # uso ya respeta los que existan, así que no hace falta comprobarlo aquí.
                    backfill_repo = DocumentRepository(db)
                    publicacion = await PublishDocumentFilesUseCase(backfill_repo).execute(
                        existing_doc.id, tenant_slug=tenant_slug
                    )
                    if publicacion["warnings"]:
                        storage_status = "error"
                        storage_error = "; ".join(publicacion["warnings"])
                    elif publicacion["uploaded"]:
                        storage_status = "ok"
                        refreshed = backfill_repo.get_by_id(existing_doc.id)
                        if refreshed is not None:
                            cloud_backed = _is_cloud_backed(
                                refreshed.pdf_storage_locations
                            ) or _is_cloud_backed(refreshed.xml_storage_locations)
                    logger.info("PDF/XML oficial backfill en documento existente %s", doc_number)
            except Exception as ex:
                db.rollback()
                storage_status = "error"
                storage_error = f"{type(ex).__name__}: {ex}"
                logger.warning("No se pudo hacer backfill de PDF/XML oficial: %s", ex)
            already_has_entry = await _has_accounting_entry(existing_doc.id)
            if not already_has_entry:
                logger.info("Documento duplicado %s sin causación — generando...", doc_number)
                acc_status, acc_error = await _trigger_accounting(existing_doc.id)
            else:
                logger.info("Documento duplicado %s ya tiene causación — omitiendo", doc_number)

        log_repo.create(
            ProcessingLog(
                filename=file_path.name,
                xml_filename=xml_filename,
                status="duplicate",
                document_number=doc_number,
                accounting_status=acc_status,
                accounting_error=acc_error,
                storage_status=storage_status,
                storage_error=storage_error,
            )
        )
        _finalize_local_files(file_path, processed_dir, cloud_backed)
        if not cloud_backed:
            logger.info("ZIP duplicado → XML: %s — movido a processed/", xml_filename)

        if progress:
            await progress.mark_xml_done(
                job_id, "duplicate", document_id=existing_doc.id if existing_doc else None
            )
            if acc_status:
                await progress.mark_accounting_done(job_id, acc_status, error=acc_error)

    except Exception as e:
        with contextlib.suppress(Exception):
            log_repo = ProcessingLogRepository(db)
            log_repo.create(
                ProcessingLog(
                    filename=file_path.name,
                    xml_filename=xml_filename,
                    status="error",
                    error_message=str(e),
                )
            )
        shutil.move(str(file_path), str(errors_dir / file_path.name))
        logger.error(
            "Error procesando %s (XML: %s): %s — movido a errors/", file_path.name, xml_filename, e
        )

        if progress:
            await progress.mark_xml_done(job_id, "error", error=str(e), error_code=type(e).__name__)

    finally:
        db.close()


async def _has_accounting_entry(document_id: int) -> bool:
    """Consulta llm-service para saber si el documento ya tiene un asiento contable."""
    url = os.getenv("LLM_SERVICE_URL", "http://llm-service:8003")
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            response = await client.get(f"{url}/api/v1/accounting/entries/{document_id}")
        if response.status_code == 200:
            data = response.json()
            return data.get("accounting_entry") is not None
    except Exception as e:
        logger.warning("No se pudo verificar causación para doc %d: %s", document_id, e)
    return False


async def _trigger_accounting(document_id: int):
    """Dispara la generación de asiento contable en llm-service (best-effort).
    Retorna (accounting_status, accounting_error)."""
    url = os.getenv("LLM_SERVICE_URL", "http://llm-service:8003")
    try:
        async with httpx.AsyncClient(timeout=30.0) as client:
            response = await client.post(
                f"{url}/api/v1/accounting/code-assignments/{document_id}",
            )
        if response.status_code in (200, 201):
            logger.info("Causación contable generada para documento %d", document_id)
            return "triggered", None
        else:
            msg = f"HTTP {response.status_code}: {response.text[:200]}"
            logger.warning("Error al generar causación para doc %d: %s", document_id, msg)
            return "error", msg
    except Exception as e:
        msg = str(e)
        logger.warning("No se pudo generar causación para doc %d: %s", document_id, msg)
        return "error", msg
