import logging
from datetime import date
from typing import Optional

import httpx
from fastapi import HTTPException, status

from app.infrastructure.clients.http_pool import get_client

logger = logging.getLogger(__name__)


class DocumentClient:
    """Cliente HTTP para obtener documentos desde xml-processor."""

    def __init__(
        self,
        base_url: str,
        bearer_token: str = "",
        internal_secret: str = "",
        tenant_slug: str = "",
    ):
        self._base_url = base_url.rstrip("/")
        self._headers = {"Authorization": f"Bearer {bearer_token}"} if bearer_token else {}
        if internal_secret:
            self._headers["x-internal-secret"] = internal_secret
        if tenant_slug:
            self._headers["x-tenant-slug"] = tenant_slug

    def _raise_connection_error(self, exc: Exception) -> None:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=f"No se pudo conectar a xml-processor ({self._base_url}): {exc}",
        )

    async def get_document(self, document_id: int) -> Optional[dict]:
        try:
            client = await get_client()
            response = await client.get(
                f"{self._base_url}/api/v1/documents/{document_id}",
                headers=self._headers,
                timeout=15.0,
            )
        except httpx.TransportError as exc:
            self._raise_connection_error(exc)
        if response.status_code == 404:
            return None
        response.raise_for_status()
        return response.json()

    async def get_document_full(self, document_id: int) -> Optional[dict]:
        """GET /internal/documents/{id}/full — retorna documento con details enriquecidos."""
        try:
            client = await get_client()
            response = await client.get(
                f"{self._base_url}/internal/documents/{document_id}/full",
                headers=self._headers,
                timeout=15.0,
            )
        except httpx.TransportError as exc:
            self._raise_connection_error(exc)
        if response.status_code == 404:
            return None
        response.raise_for_status()
        return response.json()

    async def get_issuer(self, nit: str) -> Optional[dict]:
        """GET /internal/issuers/{nit} — datos fiscales del tercero.

        RF-08: aporta el `tipo_contribuyente` (responsabilidades fiscales DIAN), que es lo
        que determina qué retenciones proceden. Best-effort: si el emisor no está
        registrado o el servicio falla, se devuelve None y la sugerencia sigue sin ese dato.
        """
        if not nit:
            return None
        try:
            client = await get_client()
            response = await client.get(
                f"{self._base_url}/internal/issuers/{nit}",
                headers=self._headers,
                timeout=10.0,
            )
        except httpx.TransportError as exc:
            logger.warning("No se pudo consultar el emisor %s: %s", nit, exc)
            return None
        if response.status_code != 200:
            return None
        return response.json()

    async def patch_detail_codes(
        self, document_id: int, assignments: list[dict], product_detection_ran: bool = False
    ) -> int:
        """PATCH /internal/documents/{id}/details — actualiza code y type en líneas de detalle.

        `product_detection_ran` avisa al xml-processor de que se evaluaron productos de verdad
        (catálogo consultado sin error). Solo entonces puede fijar el modo de asignación de un
        tercero sin definir; sin el aviso, no concluye nada.

        Retorna cantidad de filas actualizadas. Best-effort: retorna 0 en fallo.
        """
        try:
            client = await get_client()
            response = await client.patch(
                f"{self._base_url}/internal/documents/{document_id}/details",
                json=assignments,
                params={"product_detection_ran": "true"} if product_detection_ran else None,
                headers=self._headers,
                timeout=10.0,
            )
            response.raise_for_status()
            return response.json().get("updated", 0)
        except Exception as exc:
            logger.warning(
                "No se pudo actualizar códigos de detalle para doc=%s: %s", document_id, exc
            )
            return 0

    async def create_document_taxes(self, document_id: int, retentions: list[dict]) -> dict:
        """POST /internal/documents/{id}/taxes — persiste retenciones sugeridas por la IA.

        RF-08: cuando la determinación corre automáticamente al procesar el XML no hay
        interfaz esperando la respuesta, así que la propuesta debe quedar guardada para que
        el contador la encuentre en la sección de retenciones.

        Best-effort: un fallo aquí no puede tumbar el procesamiento del documento, que ya
        está guardado. Retorna contadores en cero si no se pudo persistir.
        """
        try:
            client = await get_client()
            response = await client.post(
                f"{self._base_url}/internal/documents/{document_id}/taxes",
                json=retentions,
                headers=self._headers,
                timeout=10.0,
            )
            response.raise_for_status()
            return response.json()
        except Exception as exc:
            logger.warning(
                "No se pudieron guardar las retenciones sugeridas para doc=%s: %s",
                document_id,
                exc,
            )
            return {"created": 0, "skipped": 0}

    async def list_by_date_range(
        self,
        dateini: date,
        datefin: date,
        status_filter: Optional[str] = None,
    ) -> list[dict]:
        params = {"from_date": dateini.isoformat(), "to_date": datefin.isoformat()}
        try:
            client = await get_client()
            response = await client.get(
                f"{self._base_url}/api/v1/documents",
                params=params,
                headers=self._headers,
                timeout=30.0,
            )
        except httpx.TransportError as exc:
            self._raise_connection_error(exc)
        response.raise_for_status()
        return response.json()
