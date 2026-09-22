import logging
import os
from typing import Optional

import httpx

logger = logging.getLogger(__name__)

#: Ruta del catálogo cuando la llamada nace de una petición de usuario, con su JWT.
_RUTA_CON_TOKEN = "/api/v1/integrations/taxes"  # noqa: S105 — es una ruta HTTP, no un secreto

#: Ruta del catálogo cuando la llamada nace de un proceso en segundo plano, sin usuario.
_RUTA_INTERNA = "/internal/taxes"


class IntegrationConfigClient:
    """Cliente HTTP para consultar catálogos del integration-config-service.

    Dos formas de autenticar, según de dónde nazca la llamada
    ---------------------------------------------------------
    - **Con `bearer_token`** — la llamada viene de una petición del usuario y viaja con su
      JWT por la ruta pública del servicio.
    - **Con `tenant_slug`** — la llamada nace de la descarga masiva desde la DIAN, que corre
      en segundo plano y no tiene ningún usuario detrás. Se usa el canal interno
      (`X-Internal-Secret` + `X-Tenant-Slug`) que ya emplean el siigo-service y el
      rag-service.

    Por qué existe la segunda forma
    -------------------------------
    Casi todos los documentos entran por la descarga masiva. Por esa vía el catálogo no
    llegaba nunca: el caso de uso se construía sin cliente, y cuando lo tenía, la ruta con
    token respondía 403 y el `except` lo convertía en una lista vacía. El resultado medido en
    la base del cliente fue que, de 152 líneas al 19 %, **una sola** quedó con `tax_id`.

    Ese enlace no es decorativo: es lo que permite a la interfaz mostrar el impuesto de la
    línea y lo que hace que el envío respete el impuesto que el contador eligió.
    """

    def __init__(
        self,
        base_url: str,
        bearer_token: str = "",
        tenant_slug: Optional[str] = None,
    ):
        self._base_url = base_url.rstrip("/")
        self._tenant_slug = tenant_slug
        if tenant_slug:
            # El secreto se lee en cada construcción, no en el import: así un cambio de
            # configuración surte efecto reiniciando el proceso, y los tests pueden alterarlo.
            self._headers = {
                "X-Internal-Secret": os.getenv("INTERNAL_SECRET", ""),
                "X-Tenant-Slug": tenant_slug,
            }
        elif bearer_token:
            self._headers = {"Authorization": f"Bearer {bearer_token}"}
        else:
            self._headers = {}

    @property
    def headers(self) -> dict:
        """Cabeceras con las que viaja cada petición. Expuesto para poder verificarlo."""
        return dict(self._headers)

    @property
    def taxes_path(self) -> str:
        """Ruta del catálogo, según el canal por el que hable este cliente."""
        return _RUTA_INTERNA if self._tenant_slug else _RUTA_CON_TOKEN

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
        """Catálogo de impuestos del tenant.

        Sigue siendo best-effort —un catálogo inalcanzable no puede impedir que el XML se
        guarde—, pero el fallo **se registra con la URL y el motivo**. La versión anterior
        devolvía `[]` sin dejar constancia, y por eso el defecto vivió sin que nadie lo viera:
        el documento quedaba guardado, sin error visible, y con todas sus líneas sin impuesto.
        """
        params = {"active": "true"} if active_only else {}
        url = f"{self._base_url}{self.taxes_path}"
        try:
            async with httpx.AsyncClient(timeout=5.0) as client:
                response = await client.get(url, params=params, headers=self._headers)
                response.raise_for_status()
                catalogo = response.json()
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "No se pudo obtener el catálogo de impuestos en %s (%s: %s). Las líneas del "
                "documento quedarán sin `tax_id`, así que la interfaz no mostrará su impuesto "
                "y el envío no podrá respetar el que el contador eligiera.",
                url,
                type(exc).__name__,
                exc,
            )
            return []

        if not catalogo:
            # Un catálogo vacío no es un error de red, pero tiene el mismo efecto sobre las
            # líneas. Se avisa para que no se confunda con «no hay impuestos que aplicar».
            logger.warning(
                "El catálogo de impuestos en %s vino vacío. Revise que la sincronización con "
                "SIIGO se haya ejecutado para este cliente.",
                url,
            )
        return catalogo
