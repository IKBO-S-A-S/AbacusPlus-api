import logging
import os

import httpx

from app.domain.ports.services import ProcessedDocumentsPort

logger = logging.getLogger(__name__)

_LOTE = 500  # máximo de CUFE por consulta que acepta xml-processor


class HttpProcessedDocumentsClient(ProcessedDocumentsPort):
    """Pregunta a xml-processor qué documentos del tenant ya están guardados completos.

    Servicio-a-servicio: `X-Internal-Secret` y el `X-Tenant-Slug` del token del usuario. Es
    **best-effort**: si la consulta falla devuelve un conjunto vacío y la descarga sigue como
    antes, porque xml-processor igualmente descarta los duplicados. Perder la optimización
    cuesta tiempo; abortar la descarga costaría el trabajo del usuario.
    """

    def __init__(self, base_url: str | None = None, timeout: float = 10.0):
        self._base_url = (
            base_url or os.getenv("XML_PROCESSOR_URL", "http://xml-processor:8001")
        ).rstrip("/")
        self._timeout = timeout

    async def existing_track_ids(self, tenant_slug: str, track_ids: list[str]) -> set[str]:
        existing: set[str] = set()
        try:
            async with httpx.AsyncClient(timeout=self._timeout) as client:
                for i in range(0, len(track_ids), _LOTE):
                    response = await client.post(
                        f"{self._base_url}/internal/documents/existence-checks",
                        json={"cufes": track_ids[i : i + _LOTE]},
                        headers={
                            "X-Internal-Secret": os.getenv("INTERNAL_SECRET", ""),
                            "X-Tenant-Slug": tenant_slug,
                        },
                    )
                    response.raise_for_status()
                    existing.update(str(c).lower() for c in response.json().get("existing", []))
        except Exception as exc:
            logger.warning(
                "No se pudo consultar documentos existentes (tenant=%s): %s — se descargará todo",
                tenant_slug,
                exc,
            )
            return set()
        return existing
