"""Cómo se asignan las líneas de las facturas de un tercero: por cuenta o por producto.

El tercero guarda `item_assignment_mode`:

- `account`: siempre por cuenta PUC. Se ignora la detección de productos.
- `product`: cada línea se busca en el catálogo; la que no case, o no sea un producto, va por
  cuenta, así que un mismo documento puede mezclar ambos tipos.
- `None` (sin definir): el sistema lo decide con la primera factura que se le asigne.

Este módulo solo decide **qué modo fijar** tras una asignación del modelo. Quién asigna qué a
cada línea es del llm-service; aquí no se toca.
"""

from typing import Iterable, Optional

MODE_ACCOUNT = "account"
MODE_PRODUCT = "product"
VALID_MODES = frozenset({MODE_ACCOUNT, MODE_PRODUCT})

_TYPE_PRODUCT = "Product"


def modo_a_fijar(
    modo_actual: Optional[str],
    tipos_asignados: Iterable[str],
    deteccion_corrio: bool,
) -> Optional[str]:
    """Modo que debe quedar guardado en un tercero sin definir; `None` si no hay que tocarlo.

    - Un tercero con modo ya definido no se toca nunca: ni el modelo ni una factura distinta
      cambian lo que el contador o una asignación previa dejaron.
    - `tipos_asignados`: `type` de las líneas que el modelo asignó y que se aceptaron. Sin
      ninguna (el modelo falló o se descartó todo) no hay evidencia y no se fija nada.
    - `deteccion_corrio`: el llm-service confirma que llegó a evaluar productos (consultó el
      catálogo sin error, esté vacío o no). Sin esa confirmación una caída transitoria del
      catálogo haría pasar por «no hay productos» lo que fue «no se pudo mirar», y el tercero
      quedaría fijado en cuenta para siempre por un fallo de red.
    - Con al menos una línea de producto el tercero pasa a `product`. Si la detección corrió y
      ninguna línea casó —o no hay catálogo—, pasa a `account`.
    """
    if modo_actual in VALID_MODES:
        return None
    if not deteccion_corrio:
        return None
    tipos = list(tipos_asignados)
    if not tipos:
        return None
    return MODE_PRODUCT if _TYPE_PRODUCT in tipos else MODE_ACCOUNT
