"""Resolución del medio de pago de un documento recién procesado.

Orden de precedencia, de más a menos explícito:

1. El medio de pago parametrizado en el tercero (`issuers.payment_id`). Si existe, manda y
   no se consulta ningún historial: es lo que el contador dejó configurado a propósito.
2. El más frecuente en los documentos contabilizados del mismo tercero.
3. El más frecuente en documentos contabilizados de otros terceros con conceptos parecidos.

Solo el paso 2 se escribe de vuelta en el tercero, y solo cuando el campo estaba vacío. El 3
es una conjetura por analogía: fijarla en el maestro la volvería permanente y el paso 1 la
trataría después como una decisión del contador.

El historial se limita a documentos CONTABILIZADOS (igual que el RAG, RF-08): un documento
que heredó o infirió su medio de pago no puede servir de precedente de sí mismo.
"""

from dataclasses import dataclass
from typing import Callable, Optional

SOURCE_ISSUER = "issuer"
SOURCE_ISSUER_HISTORY = "issuer_history"
SOURCE_SIMILAR_DOCUMENTS = "similar_documents"


@dataclass(frozen=True)
class PaymentTypeResolution:
    payment_type_id: int
    source: str

    @property
    def persist_on_issuer(self) -> bool:
        """Solo lo inferido del historial del propio tercero se guarda en el tercero."""
        return self.source == SOURCE_ISSUER_HISTORY


def resolve_payment_type(
    issuer_payment_id: Optional[int],
    issuer_history: Callable[[], Optional[int]],
    similar_documents: Callable[[], Optional[int]],
) -> Optional[PaymentTypeResolution]:
    """Primer candidato disponible según la precedencia; None si no hay ninguno.

    Los historiales llegan como funciones para consultarlos de forma perezosa: con el campo
    del tercero parametrizado no se toca la base, y la búsqueda por analogía solo corre si
    el historial del propio tercero no dio nada.
    """
    if issuer_payment_id is not None:
        return PaymentTypeResolution(issuer_payment_id, SOURCE_ISSUER)
    from_issuer = issuer_history()
    if from_issuer is not None:
        return PaymentTypeResolution(from_issuer, SOURCE_ISSUER_HISTORY)
    from_similar = similar_documents()
    if from_similar is not None:
        return PaymentTypeResolution(from_similar, SOURCE_SIMILAR_DOCUMENTS)
    return None
