import logging
from typing import Optional

from app.infrastructure.clients.catalog_cache import catalog_cache
from app.infrastructure.clients.http_pool import get_client

logger = logging.getLogger(__name__)


#: Ruta pública -> ruta interna equivalente. Solo se usa en el modo servicio-a-servicio.
_INTERNAL_PATHS = {
    "/api/v1/integrations/chart-accounts": "/internal/chart-accounts",
    "/api/v1/integrations/cost-centers": "/internal/cost-centers",
    "/api/v1/integrations/taxes": "/internal/taxes",
    "/api/v1/integrations/retentions": "/internal/retentions",
    "/api/v1/integrations/products": "/internal/products",
    "/api/v1/integrations/fiscal-profile": "/internal/fiscal-profile",
    "/api/v1/integrations/retention-criteria": "/internal/retention-criteria",
}


class IntegrationConfigClient:
    """Cliente HTTP para consultar catálogos del integration-config-service.

    Dos modos de autorización:

    - **Con token** (`bearer_token`): la vía interactiva. `tenant_slug` viene del token ya
      validado y solo se usa como clave de la caché de catálogos: no se envía como cabecera.
    - **Servicio-a-servicio** (`internal_secret` + `tenant_slug`): la causación automática de
      un documento descargado de la DIAN, que corre en segundo plano sin JWT de usuario. Va a
      las rutas `/internal/*`, con `X-Internal-Secret` y `X-Tenant-Slug`.

    Sin `tenant_slug` el cliente funciona igual pero sin caché.
    """

    def __init__(
        self,
        base_url: str,
        bearer_token: str = "",
        tenant_slug: str = "",
        internal_secret: str = "",
    ):
        self._base_url = base_url.rstrip("/")
        self._headers = {"Authorization": f"Bearer {bearer_token}"} if bearer_token else {}
        self._tenant_slug = tenant_slug
        self._internal = bool(internal_secret)
        if self._internal:
            self._headers = {"X-Internal-Secret": internal_secret, "X-Tenant-Slug": tenant_slug}

    async def _get_json(self, path: str, params: Optional[dict] = None, timeout: float = 5.0):
        params = dict(params or {})
        if self._internal:
            path = _INTERNAL_PATHS.get(path, path)
            if path == "/internal/taxes":
                # El catálogo completo, como la ruta pública: el filtro por activo lo aplica
                # quien consume, porque las filas inactivas siguen sirviendo para reconocer
                # el tipo de una retención ya registrada.
                params["include_inactive"] = "true"
        client = await get_client()
        response = await client.get(
            f"{self._base_url}{path}",
            params=params,
            headers=self._headers,
            timeout=timeout,
        )
        response.raise_for_status()
        return response.json()

    async def get_chart_accounts(self, active_only: bool = True) -> list[dict]:
        """Retorna el plan de cuentas configurado.

        Cacheado por empresa: es un catálogo de configuración, idéntico para todos los
        documentos del mismo cliente, y se pedía entero una vez por documento causado.

        Llamada best-effort: retorna lista vacía si el servicio no está disponible. Ese vacío
        nunca se cachea (ver `catalog_cache`), para que una caída momentánea no se prolongue.
        """
        params = {"active": "true"} if active_only else {}

        async def _cargar() -> list[dict]:
            try:
                return await self._get_json("/api/v1/integrations/chart-accounts", params=params)
            except Exception as exc:
                logger.warning(
                    "No se pudo obtener plan de cuentas de integration-config-service: %s", exc
                )
                return []

        return await catalog_cache.get_or_load(
            self._tenant_slug, f"chart_accounts:{active_only}", _cargar
        )

    async def get_taxes(self) -> list[dict]:
        """RF-08: catálogo de impuestos y retenciones sincronizado con SIIGO.

        Es la fuente autorizada de los porcentajes: el modelo elige qué retención aplica,
        pero la tarifa se toma siempre de aquí. Llamada best-effort, como el resto.
        """

        async def _cargar() -> list[dict]:
            try:
                return await self._get_json("/api/v1/integrations/taxes")
            except Exception as exc:
                logger.warning("No se pudo obtener el catálogo de impuestos: %s", exc)
                return []

        return await catalog_cache.get_or_load(self._tenant_slug, "taxes", _cargar)

    async def get_retentions(self) -> list[dict]:
        """RF-08: catálogo de retenciones (ReteFuente, ReteICA, ReteIVA, Autorretención).

        Separado de `get_taxes()` desde la migración del 2026-08-31: antes las retenciones
        vivían mezcladas con los impuestos reales del documento en `integration_taxes`; ahora
        tienen su propia tabla física (`integration_retentions`), y cada fila `type='reteica'`
        trae además su municipio, concepto y base mínima en la misma fila — antes esa
        información solo existía en una tabla paralela del xml-processor
        (`retention_ica_rates`) que casi nunca coincidía en porcentaje con el catálogo plano,
        así que muchas tarifas reales no podían proponerse. La tarifa sigue siendo la fuente
        autorizada del porcentaje; el modelo solo elige cuál aplica.

        Se piden TODAS (activas e inactivas), igual que `get_taxes()`: quien filtra por activo
        es `retention_candidates()`, y las inactivas siguen haciendo falta para resolver el
        tipo de una retención ya registrada aunque su fila se haya desactivado después
        (`_excluding_registered_types`). Llamada best-effort, como el resto.
        """

        async def _cargar() -> list[dict]:
            try:
                return await self._get_json("/api/v1/integrations/retentions")
            except Exception as exc:
                logger.warning("No se pudo obtener el catálogo de retenciones: %s", exc)
                return []

        return await catalog_cache.get_or_load(self._tenant_slug, "retentions", _cargar)

    async def get_products(self) -> Optional[list[dict]]:
        """Productos activos del catálogo (los servicios quedan fuera: se causan por cuenta).

        A diferencia del resto de catálogos, distingue **dos** estados que `[]` confundiría:

        - lista vacía: la consulta funcionó y el cliente no tiene productos cargados;
        - `None`: no se pudo consultar.

        La diferencia es de fondo. Con catálogo vacío el tercero queda causado por cuenta; con
        el servicio caído, no se sabe nada, y fijarle «por cuenta» a un tercero por una caída
        de red lo dejaría sin productos para siempre. Quien llama usa el `None` para no
        concluir nada.

        Cacheado por empresa. Un resultado vacío o fallido no se cachea (ver `catalog_cache`).
        """
        falló = False

        async def _cargar() -> list[dict]:
            nonlocal falló
            try:
                return await self._get_json(
                    "/api/v1/integrations/products", params={"active": "true", "type": "product"}
                )
            except Exception as exc:
                falló = True
                logger.warning("No se pudo obtener el catálogo de productos: %s", exc)
                return []

        productos = await catalog_cache.get_or_load(self._tenant_slug, "products", _cargar)
        return None if falló else productos

    async def get_fiscal_profile(self) -> Optional[dict]:
        """Perfil fiscal del tenant (el COMPRADOR): define si la empresa es agente de retención.

        Es autoritativo sobre el `TaxLevelCode` del receptor en el XML. Best-effort: si no está
        disponible, se devuelve None y la decisión cae al dato del XML.
        """
        try:
            return await self._get_json("/api/v1/integrations/fiscal-profile")
        except Exception as exc:
            logger.warning("No se pudo obtener el perfil fiscal del tenant: %s", exc)
            return None

    async def get_cost_centers(self) -> list[dict]:
        """Retorna los centros de costo configurados.

        Su nombre da contexto al modelo sobre el área que consume el gasto (p. ej.
        «Gastos de Personal» o «Desarrollo de plataformas»), que ayuda a desambiguar
        descripciones genéricas. Llamada best-effort: la asignación no depende de esto.

        Cacheado por empresa por la misma razón que el plan de cuentas.
        """

        async def _cargar() -> list[dict]:
            try:
                return await self._get_json("/api/v1/integrations/cost-centers")
            except Exception as exc:
                logger.warning(
                    "No se pudo obtener centros de costo de integration-config-service: %s", exc
                )
                return []

        return await catalog_cache.get_or_load(self._tenant_slug, "cost_centers", _cargar)

    async def get_retention_criteria(self) -> list[dict]:
        """RF-08: criterios del contador de ESTA empresa sobre cómo determinar retenciones.

        Son datos por tenant, no una configuración global: cada contador tiene su criterio y
        estos cambian con la norma o con su interpretación. Por eso se consultan al servicio
        en cada sugerencia en vez de estar escritos en el código de este servicio, donde
        cambiar uno exigiría un despliegue y se aplicaría a todos los clientes por igual.

        Se piden TODOS y entran todos al prompt: no es una recuperación por relevancia. Son
        reglas que gobiernan cada factura, y hacerlas depender de una búsqueda semántica
        significaría que algún día no llegan y el modelo decide sin ellas sin que se note.

        Best-effort, como el resto del cliente: sin criterios la sugerencia se apoya en las
        tablas oficiales y el perfil fiscal, que son las fuentes vinculantes.
        """
        try:
            payload = await self._get_json("/api/v1/integrations/retention-criteria")
            return payload.get("criterios", [])
        except Exception as exc:
            logger.warning("RF-08: no se pudieron obtener los criterios del contador: %s", exc)
            return []
