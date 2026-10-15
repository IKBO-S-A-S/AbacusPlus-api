import logging
from typing import Optional

logger = logging.getLogger(__name__)


class AutoAccountingUseCase:
    """Causación automática de un documento recién guardado: cuentas PUC y retenciones.

    Es la política de CUÁNDO se dispara, separada del cliente que la ejecuta, para que la
    compartan la carga manual de un XML y la descarga automática desde la DIAN.

    Solo cuando ya existe historial confirmado por el contador. En la primera descarga de un
    tenant no hay ninguna cuenta validada por una persona, así que el modelo no tendría
    precedente sobre el que apoyarse: el documento se deja tal como llegó y el contador
    decide, asignando a mano o pidiendo la sugerencia de forma explícita desde la interfaz.

    Best-effort: el documento ya está guardado y un fallo del llm-service no puede perderlo.
    """

    def __init__(self, document_repo, llm_client):
        self._repo = document_repo
        self._llm = llm_client

    async def execute(
        self, document_id: int, only_if_uncoded: bool = False
    ) -> tuple[Optional[str], Optional[str]]:
        """Devuelve `(estado, error)`.

        `estado` es `"triggered"` o `"error"` si se intentó la causación, y `None` si no
        aplicaba (sin cliente, sin historial confirmado, o documento ya causado).
        `only_if_uncoded` lo usa el reproceso de duplicados: un documento que ya tiene cuentas
        no debe volver a gastar una llamada al modelo.
        """
        if self._llm is None:
            return None, None

        if only_if_uncoded:
            doc = self._repo.get_by_id(document_id)
            if doc is not None and any(d.code for d in doc.details):
                logger.info("Documento %d ya tiene cuentas asignadas — se omite la causación", document_id)
                return None, None

        if not self._repo.has_confirmed_accounting_history():
            logger.info(
                "Documento %d guardado sin asignación automática: el tenant aún no "
                "tiene contabilizaciones confirmadas por el usuario.",
                document_id,
            )
            return None, None

        status, error = await self._llm.trigger_code_assignment(document_id)
        # RF-08: la IA determina las retenciones del tercero durante el procesamiento del
        # documento, no solo cuando el contador pulsa el botón. Va después de la asignación
        # de cuentas porque ambas comparten el prerrequisito del PUC: si falta, la primera
        # ya habrá dejado el aviso en el log.
        await self._llm.trigger_retention_suggestion(document_id)
        return status, error
