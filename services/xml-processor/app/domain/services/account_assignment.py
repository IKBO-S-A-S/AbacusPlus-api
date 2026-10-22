"""Reglas de validación de una asignación contable a una línea de documento.

Existe un único punto de verdad porque hay **dos** vías que escriben `document_details.code`:
la ruta pública que consume la interfaz (edición del contador, RF-01) y la ruta interna que
consume el llm-service (sugerencia del modelo, RF-04). Antes solo la primera validaba, y por
eso el modelo pudo persistir cuentas inexistentes en el catálogo, cuentas de clase 4
(ingresos) e incluso códigos con formato ajeno al PUC del cliente.

Las reglas son de dominio —qué constituye una imputación contable admisible— y no dependen
de FastAPI ni de SQLAlchemy, así que viven aquí y cada adaptador decide qué hacer con el
resultado: la ruta pública responde 422 (el contador debe enterarse de su error) y la interna
descarta la línea y sigue (una sugerencia inválida no debe romper el procesamiento del XML).
"""

from dataclasses import dataclass
from typing import Optional

# Clases PUC que pueden recibir la contrapartida del ítem de un documento de compra:
# 5 Gastos · 6 Costos de venta · 7 Costos de producción o de operación.
# Se excluyen 2 (Pasivo) y 3 (Patrimonio) —propias de la cuenta por pagar y de las
# retenciones— y 4 (Ingresos), que es lo que la empresa factura, no lo que compra.
ITEM_ACCOUNT_CLASSES = frozenset({"5", "6", "7"})

# La clase 1 (Activo) admite un ítem solo en algunos grupos: comprar mercancía, un activo
# fijo, un intangible o un gasto anticipado. Nunca Efectivo (11), Inversiones (12),
# Deudores (13) ni Valorizaciones (19): una compra no se imputa a la caja ni a una cuenta
# por cobrar.
ITEM_ASSET_GROUPS = frozenset({"14", "15", "16", "17", "18"})

#: `type` de una línea cuyo `code` es un producto del catálogo y no una cuenta PUC.
TYPE_PRODUCT = "Product"
TYPE_ACCOUNT = "Account"


@dataclass(frozen=True)
class RejectedAssignment:
    """Una asignación descartada, con el motivo en términos que el usuario entiende."""

    detail_id: Optional[int]
    code: Optional[str]
    reason: str


@dataclass(frozen=True)
class ValidationOutcome:
    accepted: list  # asignaciones que pueden persistirse, tal como llegaron
    rejected: list  # list[RejectedAssignment]


def is_item_account(code: str) -> bool:
    """True si el código puede ser la contrapartida del ítem de una factura de compra.

    El criterio es la posición en el PUC (clase y grupo), estándar y estable, y no cómo
    esté rotulado `account_type` en el catálogo de cada cliente.
    """
    if not code:
        return False
    if code[0] in ITEM_ACCOUNT_CLASSES:
        return True
    return code[0] == "1" and code[:2] in ITEM_ASSET_GROUPS


def validate_assignments(
    assignments: list,
    own_detail_ids: set,
    puc_index: dict,
    valid_cost_center_ids: set,
    enforce_item_class: bool = False,
    product_index: Optional[dict] = None,
) -> ValidationOutcome:
    """Filtra las asignaciones admisibles y explica cada descarte.

    - `own_detail_ids`: líneas que pertenecen al documento de la ruta. Acota el alcance de
      la operación al documento en curso y descarta cualquier `detail_id` ajeno.
    - `puc_index`: catálogo del tenant indexado por código. Cada entrada debe traer
      `is_active` y `accepts_movements`.
    - `product_index`: catálogo de productos indexado por código (cada entrada con
      `is_active`). Solo se consulta para las asignaciones con `type="Product"`.
    - `enforce_item_class`: exige además que la cuenta sea imputable a un ítem de compra.
      Se activa para las sugerencias del modelo; la edición manual no lo aplica porque el
      contador puede tener un caso legítimo que las reglas generales no contemplan.

    Un catálogo vacío desactiva la comprobación de existencia —un tenant recién instalado
    aún no ha importado su PUC y bloquearlo impediría trabajar—, pero nunca desactiva el
    acotamiento por documento, que es una garantía de aislamiento y no una conveniencia.
    """
    accepted: list = []
    rejected: list = []

    for assignment in assignments:
        detail_id = _get(assignment, "detail_id")
        code = _get(assignment, "code")

        if detail_id not in own_detail_ids:
            rejected.append(
                RejectedAssignment(
                    detail_id=detail_id,
                    code=code,
                    reason="La línea no pertenece a este documento.",
                )
            )
            continue

        # Una línea de producto no lleva cuenta PUC sino el código del catálogo de productos:
        # se valida contra ese catálogo y se omiten las reglas de clase PUC, que no le aplican.
        # A diferencia del PUC, un catálogo vacío NO desactiva la comprobación: sin catálogo no
        # hay forma de saber que el código es un producto y no un texto cualquiera.
        is_product = code is not None and _get(assignment, "type") == TYPE_PRODUCT
        if is_product:
            product = (product_index or {}).get(code)
            if product is None:
                rejected.append(
                    RejectedAssignment(
                        detail_id=detail_id,
                        code=code,
                        reason=f"El producto '{code}' no existe en el catálogo de productos.",
                    )
                )
                continue
            if not product.get("is_active", True):
                rejected.append(
                    RejectedAssignment(
                        detail_id=detail_id,
                        code=code,
                        reason=f"El producto '{code}' está inactivo en el catálogo.",
                    )
                )
                continue

        # `code` ausente o nulo es legítimo: significa «no tocar la cuenta» o «limpiarla».
        # Solo se valida cuando trae valor.
        if code is not None and puc_index and not is_product:
            account = puc_index.get(code)
            if account is None:
                rejected.append(
                    RejectedAssignment(
                        detail_id=detail_id,
                        code=code,
                        reason=f"La cuenta '{code}' no existe en el catálogo PUC sincronizado.",
                    )
                )
                continue
            if not account.get("is_active", True):
                rejected.append(
                    RejectedAssignment(
                        detail_id=detail_id,
                        code=code,
                        reason=f"La cuenta '{code}' está inactiva en el catálogo.",
                    )
                )
                continue
            # None = el catálogo no informa el dato; no se bloquea por desconocimiento.
            if account.get("accepts_movements") is False:
                rejected.append(
                    RejectedAssignment(
                        detail_id=detail_id,
                        code=code,
                        reason=(
                            f"La cuenta '{code}' no admite movimiento: agrupa otras cuentas "
                            "y no se puede imputar directamente."
                        ),
                    )
                )
                continue
            if enforce_item_class and not is_item_account(code):
                rejected.append(
                    RejectedAssignment(
                        detail_id=detail_id,
                        code=code,
                        reason=(
                            f"La cuenta '{code}' no puede recibir el ítem de una compra: "
                            "solo aplican gastos (5), costos (6-7) e inventarios o activos "
                            "(14-18)."
                        ),
                    )
                )
                continue

        cost_center_id = _get(assignment, "cost_center_id")
        if (
            cost_center_id is not None
            and valid_cost_center_ids
            and cost_center_id not in valid_cost_center_ids
        ):
            rejected.append(
                RejectedAssignment(
                    detail_id=detail_id,
                    code=code,
                    reason=f"El centro de costo '{cost_center_id}' no existe en el catálogo.",
                )
            )
            continue

        accepted.append(assignment)

    return ValidationOutcome(accepted=accepted, rejected=rejected)


def _get(assignment, field: str):
    """Lee un campo tanto de un modelo Pydantic como de un diccionario.

    Las dos rutas que usan este validador reciben modelos Pydantic, pero los tests y
    cualquier consumidor futuro pueden trabajar con diccionarios.
    """
    if isinstance(assignment, dict):
        return assignment.get(field)
    return getattr(assignment, field, None)
