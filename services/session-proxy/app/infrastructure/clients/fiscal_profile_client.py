import logging
import os
from typing import Optional

import httpx

from app.domain.exceptions.base import ExternalRequestException
from app.domain.ports.services import FiscalProfilePort

logger = logging.getLogger(__name__)


class HttpFiscalProfileClient(FiscalProfilePort):
    """Lee el perfil fiscal del tenant en integration-config-service.

    Servicio-a-servicio: autentica con `X-Internal-Secret` y pasa el `X-Tenant-Slug` del token
    del usuario, igual que el llm-service. No es best-effort: si no se puede leer el NIT, no se
    puede comprobar que el enlace sea de la empresa y la descarga no debe continuar.
    """

    def __init__(self, base_url: str | None = None, timeout: float = 10.0):
        self._base_url = (
            base_url
            or os.getenv("INTEGRATION_CONFIG_URL", "http://integration-config-service:8007")
        ).rstrip("/")
        self._timeout = timeout

    async def get_nit(self, tenant_slug: str) -> Optional[str]:
        try:
            async with httpx.AsyncClient(timeout=self._timeout) as client:
                response = await client.get(
                    f"{self._base_url}/internal/fiscal-profile",
                    headers={
                        "X-Internal-Secret": os.getenv("INTERNAL_SECRET", ""),
                        "X-Tenant-Slug": tenant_slug,
                    },
                )
            response.raise_for_status()
        except httpx.HTTPError as exc:
            logger.error("No se pudo leer el perfil fiscal (tenant=%s): %s", tenant_slug, exc)
            raise ExternalRequestException(
                "No se pudo consultar el perfil fiscal de la empresa para validar el NIT."
            ) from exc
        return response.json().get("nit") or None
