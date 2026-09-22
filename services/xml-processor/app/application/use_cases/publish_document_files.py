"""RF-03 · Publicación del PDF y el XML del documento en los backends de almacenamiento
configurados por el tenant (S3 y/o SharePoint; local si no hay ninguno).

Este caso de uso concentra ese paso. Antes vivía embebido en el trabajador de descargas,
con dos consecuencias: los documentos cargados manualmente nunca obtenían enlace, y no
existía forma de reintentar la subida de un documento cuyo enlace hubiera fallado. Al
extraerlo, la misma lógica sirve a las dos rutas de ingreso y a la reparación posterior.

Los bytes se leen de la base, no del ZIP: son la fuente de verdad que ya quedó almacenada,
así que un documento se puede publicar en cualquier momento posterior a su procesamiento.

Las credenciales de S3/SharePoint son siempre las del tenant (`resolve_storage`,
`integration-config-service`) — nunca variables de entorno globales del servicio.
"""

import logging
import os
from pathlib import Path
from typing import Optional

from app.application.use_cases.resolve_storage_backends import resolve_storage
from app.domain.services.document_naming import build_document_filename
from app.infrastructure.clients.integration_config_client import IntegrationConfigClient
from app.infrastructure.persistence.models.document import Document
from app.infrastructure.persistence.repositories.document_repository import DocumentRepository
from app.infrastructure.storage.local_storage_adapter import LocalStorageAdapter
from app.infrastructure.storage.s3_storage_adapter import S3StorageAdapter

logger = logging.getLogger(__name__)


def _document_data(document: Document) -> dict:
    return {
        "issuer_nit": document.issuer_nit,
        "issuer_name": document.issuer_name,
        "receiver_nit": document.receiver_nit,
        "receiver_name": document.receiver_name,
        "document_type": document.document_type,
        "document_number": document.document_number,
        "date": document.date,
        "cufe": document.cufe,
    }


class PublishDocumentFilesUseCase:
    """Sube a los backends del tenant los archivos ya almacenados de un documento y
    persiste sus enlaces."""

    def __init__(
        self,
        document_repo: DocumentRepository,
        integration_config_client: Optional[IntegrationConfigClient] = None,
    ):
        self._repo = document_repo
        self._client = integration_config_client or IntegrationConfigClient(
            base_url=os.getenv("INTEGRATION_CONFIG_URL", "http://integration-config-service:8007")
        )

    async def execute(
        self, document_id: int, tenant_slug: str = "", overwrite: bool = False
    ) -> dict:
        """Publica el PDF y el XML de un documento.

        `overwrite=False` respeta los enlaces ya guardados: republicar un documento que ya
        está publicado gastaría ancho de banda y cambiaría una URL que el contador puede
        tener abierta. Se envía `true` cuando se quiere rehacer un enlace roto o vencido.

        Best-effort por archivo y por backend: que falle el XML no impide guardar el
        enlace del PDF, y que falle un backend no impide subir a los demás configurados.
        """
        document = self._repo.get_by_id(document_id)
        if document is None:
            raise ValueError(f"Documento {document_id} no encontrado")

        local_fallback_dir = Path(os.getenv("DOWNLOADS_DIR", "/app/downloads")) / "processed"
        storage = await resolve_storage(
            client=self._client,
            tenant_slug=tenant_slug,
            local_fallback_dir=local_fallback_dir,
        )
        base_name = build_document_filename(storage.naming_pattern, _document_data(document))

        uploaded: list[str] = []
        skipped: list[str] = []
        warnings: list[str] = []

        pdf_locations, pdf_link = await self._publish(
            base_name, "pdf", document.pdf_data, document.pdf_url, storage.backends, overwrite, skipped, warnings
        )
        if pdf_locations:
            uploaded.append("pdf")

        xml_locations, xml_link = await self._publish(
            base_name, "xml", document.xml_data, document.xml_url, storage.backends, overwrite, skipped, warnings
        )
        if xml_locations:
            uploaded.append("xml")

        if pdf_locations or xml_locations:
            self._repo.update_storage_locations(
                document_id, pdf_locations or document.pdf_storage_locations, xml_locations or document.xml_storage_locations
            )
        if uploaded:
            actualizado = self._repo.update_file_urls(document_id, pdf_url=pdf_link, xml_url=xml_link)
            document = actualizado or document

        return {
            "document_id": document_id,
            "pdf_url": document.pdf_url,
            "xml_url": document.xml_url,
            "uploaded": uploaded,
            "skipped": skipped,
            "warnings": warnings,
        }

    async def _publish(
        self,
        base_name: str,
        kind: str,
        data: Optional[bytes],
        current_url: Optional[str],
        backends: list,
        overwrite: bool,
        skipped: list[str],
        warnings: list[str],
    ) -> tuple[dict, Optional[str]]:
        """Sube un archivo concreto a cada backend configurado por el tenant.

        Retorna (locations, pdf_url_o_xml_url): `locations` es el mapa {backend: ubicación}
        de lo que se subió en esta ejecución; el segundo valor es la ubicación en S3
        específicamente (si ese backend está entre los configurados), que es el único que
        alimenta las columnas `pdf_url`/`xml_url` que consume el resto de la API.
        """
        if not data:
            skipped.append(kind)
            return {}, None
        if current_url and not overwrite:
            skipped.append(kind)
            return {}, None

        filename = f"{base_name}.{kind}"
        locations: dict = {}
        s3_link: Optional[str] = None
        for backend in backends:
            backend_name = type(backend).__name__
            # El adaptador local preserva la estructura processed/{pdf,xml}/ existente;
            # los adaptadores en la nube guardan el archivo plano bajo su prefijo/carpeta.
            save_name = f"{kind}/{filename}" if isinstance(backend, LocalStorageAdapter) else filename
            try:
                location = await backend.save(data, save_name)
                locations[backend_name] = location
                if isinstance(backend, S3StorageAdapter):
                    s3_link = location
                logger.info("%s guardado (%s) → %s", kind.upper(), backend_name, location)
            except Exception as e:
                warnings.append(
                    f"No se pudo publicar el {kind.upper()} en {backend_name}: {e}"
                )
                logger.warning("Fallo guardando %s en %s: %s", filename, backend_name, e)

        if not locations:
            return {}, None
        return locations, s3_link
