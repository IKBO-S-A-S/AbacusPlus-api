import logging
import os
from typing import Optional

import httpx

logger = logging.getLogger(__name__)


def _seconds(name: str, default: float) -> float:
    try:
        return float(os.getenv(name, default))
    except ValueError:
        return default


class LlmClient:
    """Cliente HTTP para llamar al llm-service.

    Dos modos de autorización:

    - **Con token** (`bearer_token`): la vía interactiva, con el JWT del usuario que subió el
      archivo. Usa las rutas públicas `/api/v1/accounting/*`.
    - **Servicio-a-servicio** (`internal_secret` + `tenant_slug`): la descarga automática desde
      la DIAN, que corre en un worker sin ningún usuario detrás. Usa las rutas
      `/internal/accounting/*`, con `X-Internal-Secret` y `X-Tenant-Slug`. Con el token vacío
      esta vía respondía 401 y los documentos descargados nunca recibían cuentas.

    Los tiempos de espera salen de las variables `LLM_ASSIGN_TIMEOUT_SECONDS` y
    `LLM_RETENTION_TIMEOUT_SECONDS`. Deben superar a `OPENAI_TIMEOUT_SECONDS` del llm-service
    (60 s por defecto): con 30 s esta llamada se rendía antes que el propio modelo y reportaba
    como error una asignación que luego sí se completaba.
    """

    def __init__(
        self,
        base_url: str,
        bearer_token: str = "",
        internal_secret: str = "",
        tenant_slug: str = "",
    ):
        self.base_url = base_url.rstrip("/")
        if internal_secret:
            self._headers = {"X-Internal-Secret": internal_secret, "X-Tenant-Slug": tenant_slug}
            self._accounting_url = f"{self.base_url}/internal/accounting"
        else:
            self._headers = {"Authorization": f"Bearer {bearer_token}"} if bearer_token else {}
            self._accounting_url = f"{self.base_url}/api/v1/accounting"

    async def trigger_code_assignment(self, document_id: int) -> tuple[str, Optional[str]]:
        """Dispara la asignación de cuentas PUC en llm-service. Best-effort.

        Devuelve `(estado, error)`: `("triggered", None)` si el llm-service respondió 200, o
        `("error", motivo)` si falló. Nunca lanza: el documento ya está guardado.
        """
        url = f"{self._accounting_url}/code-assignments/{document_id}"
        try:
            async with httpx.AsyncClient(timeout=_seconds("LLM_ASSIGN_TIMEOUT_SECONDS", 90.0)) as client:
                response = await client.post(url, headers=self._headers)
                if response.status_code == 200:
                    data = response.json()
                    logger.info(
                        "Asignación PUC doc=%s: assigned=%s skipped=%s",
                        document_id,
                        data.get("assigned"),
                        data.get("skipped"),
                    )
                    return "triggered", None
                motivo = f"HTTP {response.status_code}: {response.text[:200]}"
                logger.warning(
                    "llm-service retornó status %s al asignar cuentas para doc=%s",
                    response.status_code,
                    document_id,
                )
                return "error", motivo
        except Exception as exc:
            logger.warning(
                "No se pudo disparar asignación de cuentas para doc=%s: %s", document_id, exc
            )
            return "error", str(exc) or type(exc).__name__

    async def trigger_retention_suggestion(self, document_id: int) -> None:
        """RF-08: determina las retenciones del tercero al procesar el documento.

        Se envía `persist=true` porque aquí no hay interfaz esperando la respuesta: la
        propuesta debe quedar guardada en el documento para que el contador la encuentre
        en la sección de retenciones y la confirme o la ajuste.

        Best-effort, igual que la asignación de cuentas: el documento ya está guardado y un
        fallo del llm-service no puede perderlo. El 409 (sin PUC o sin catálogo de
        impuestos) es una condición esperada y se registra sin ruido de error.
        """
        url = f"{self._accounting_url}/retention-suggestions/{document_id}"
        try:
            async with httpx.AsyncClient(
                timeout=_seconds("LLM_RETENTION_TIMEOUT_SECONDS", 90.0)
            ) as client:
                response = await client.post(url, headers=self._headers, params={"persist": "true"})
                if response.status_code == 200:
                    persisted = response.json().get("persisted") or {}
                    logger.info(
                        "Retenciones automáticas doc=%s: created=%s skipped=%s",
                        document_id,
                        persisted.get("created", 0),
                        persisted.get("skipped", 0),
                    )
                elif response.status_code == 409:
                    logger.info(
                        "Retenciones automáticas omitidas para doc=%s: %s",
                        document_id,
                        response.json().get("detail"),
                    )
                else:
                    logger.warning(
                        "llm-service retornó status %s al sugerir retenciones para doc=%s",
                        response.status_code,
                        document_id,
                    )
        except Exception as exc:
            logger.warning(
                "No se pudo disparar la sugerencia de retenciones para doc=%s: %s",
                document_id,
                exc,
            )
