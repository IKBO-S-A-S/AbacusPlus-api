import logging
import os
from typing import Optional

import httpx

logger = logging.getLogger(__name__)


class IntegrationConfigClient:
    """Cliente HTTP para consultar catálogos del integration-config-service."""

    def __init__(self, base_url: str, bearer_token: str = ""):
        self._base_url = base_url.rstrip("/")
        self._headers = {"Authorization": f"Bearer {bearer_token}"} if bearer_token else {}

    async def get_storage_settings(self, tenant_slug: str) -> Optional[dict]:
        """Retorna la configuracion agregada de almacenamiento (S3/SharePoint/patron de
        nombre) del tenant, con secretos desencriptados.

        Llamada servicio-a-servicio (worker de background sin JWT de usuario): usa el
        endpoint interno protegido con X-Internal-Secret, igual que provision-tenant.
        Best-effort: retorna None si el servicio no esta disponible (=> fallback local).
        """
        try:
            async with httpx.AsyncClient(timeout=5.0) as client:
                response = await client.get(
                    f"{self._base_url}/internal/storage-settings",
                    params={"tenant_slug": tenant_slug},
                    headers={"X-Internal-Secret": os.environ.get("INTERNAL_SECRET", "")},
                )
                response.raise_for_status()
                return response.json()
        except Exception as exc:
            logger.warning(
                "No se pudo obtener configuracion de almacenamiento de integration-config-service: %s",
                exc,
            )
            return None

    async def get_taxes(self, active_only: bool = True) -> list[dict]:
        """Retorna la lista de impuestos configurados.

        Llamada best-effort: retorna lista vacía si el servicio no está disponible.
        """
        params = {"active": "true"} if active_only else {}
        try:
            async with httpx.AsyncClient(timeout=5.0) as client:
                response = await client.get(
                    f"{self._base_url}/api/v1/integrations/taxes",
                    params=params,
                    headers=self._headers,
                )
                response.raise_for_status()
                return response.json()
        except Exception as exc:
            logger.warning("No se pudo obtener impuestos de integration-config-service: %s", exc)
            return []
