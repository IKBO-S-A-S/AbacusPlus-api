import logging
from typing import Optional

from app.domain.ports.services import RagClientPort
from app.infrastructure.clients.http_pool import get_client

logger = logging.getLogger(__name__)


class RagClient(RagClientPort):
    """Cliente HTTP para consultar el rag-service."""

    def __init__(
        self,
        base_url: str,
        bearer_token: str = "",
        internal_secret: str = "",
        tenant_slug: str = "",
    ):
        self._base_url = base_url.rstrip("/")
        self._headers = {"Authorization": f"Bearer {bearer_token}"} if bearer_token else {}
        # Modo servicio-a-servicio: la causación automática corre sin JWT de usuario.
        self._internal = bool(internal_secret)
        if self._internal:
            self._headers = {"X-Internal-Secret": internal_secret, "X-Tenant-Slug": tenant_slug}

    async def search(
        self,
        query: str,
        top_k: int = 5,
        only_validated: bool = False,
        filters: Optional[dict] = None,
        min_similarity: Optional[float] = None,
    ) -> list[dict]:
        """Búsqueda semántica en el rag-service.

        RF-08: `only_validated` restringe el resultado a las causaciones que se contabilizaron
        en SIIGO. Debe ir en True siempre que el contexto vaya a sustentar una sugerencia
        contable —cuentas o retenciones—, porque un precedente que nunca llegó a ningún libro
        no es un precedente: es la propuesta anterior del propio modelo.

        `filters` es la mitad estructurada de la búsqueda híbrida de RF-08 —NIT del emisor,
        municipio, tipos de retención—, y el rag-service la aplica ANTES de ordenar por
        similitud. Sin ella, la recuperación de precedentes devuelve vecinos textuales de
        cualquier tercero, que es justo lo que no sirve para decidir una retención.

        `min_similarity` es el umbral por debajo del cual no se devuelve nada. Se deja sin
        fijar por defecto para que mande el del rag-service, que es quien conoce el modelo de
        embeddings con el que se calculó la distancia; pasarlo aquí solo tiene sentido para
        endurecerlo en una consulta concreta.
        """
        payload: dict = {"query": query, "top_k": top_k, "only_validated": only_validated}
        if filters:
            payload["filters"] = filters
        if min_similarity is not None:
            payload["min_similarity"] = min_similarity
        client = await get_client()
        response = await client.post(
            f"{self._base_url}/internal/chunks/search"
            if self._internal
            else f"{self._base_url}/api/v1/chunks/search",
            json=payload,
            headers=self._headers,
            timeout=15.0,
        )
        response.raise_for_status()
        return response.json().get("results", [])

    async def index_chunk(self, source_type: str, source_id: int, content: str) -> None:
        try:
            client = await get_client()
            response = await client.post(
                f"{self._base_url}/api/v1/chunks",
                json={"source_type": source_type, "source_id": source_id, "content": content},
                headers=self._headers,
                timeout=15.0,
            )
            response.raise_for_status()
        except Exception as e:
            logger.warning("No se pudo indexar asiento en RAG (source_id=%d): %s", source_id, e)
