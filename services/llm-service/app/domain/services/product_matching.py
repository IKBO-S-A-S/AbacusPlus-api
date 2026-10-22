"""Preselección de productos del catálogo candidatos a una línea de factura.

Cuando un tercero compra artículos inventariables, SIIGO espera un ítem `Product` con el código
del catálogo en lugar de una cuenta PUC. Decidir cuál producto es compete al modelo, pero
mandarle el catálogo entero no es viable (puede tener miles de filas) ni sensato: casi todas
serían ruido para una línea concreta. Este módulo reduce el catálogo a unos pocos candidatos
por línea, sin llamar a ningún servicio.

Dos reglas de seguridad que viven aquí y no en el prompt:

- El modelo **solo puede elegir entre los candidatos de esa línea** (`resolver_producto`). Un
  código inventado, o uno válido pero de otra línea, vuelve a la cuenta.
- La preselección es léxica y generosa a propósito: sobra con que el producto correcto esté
  entre los candidatos; que lo sea lo decide el modelo leyendo la descripción completa.
"""

import re
import unicodedata
from typing import Iterable, Optional

MODE_ACCOUNT = "account"
MODE_PRODUCT = "product"

#: Candidatos por línea. Suficientes para que el correcto esté entre ellos; pocos para que el
#: prompt no crezca con el catálogo.
DEFAULT_TOP_K = 8

#: Bonificación al producto con que ya se contabilizaron facturas del mismo tercero. Solo suma
#: sobre una coincidencia léxica: un producto del historial sin ninguna palabra en común con la
#: línea no es candidato por haberse usado antes.
HISTORY_BONUS = 0.25

# Palabras que no distinguen un artículo de otro. Con ellas «tornillo de acero» casaría con
# «cable de acero» y con cualquier descripción que contenga «de».
_STOPWORDS = frozenset(
    {
        "de", "del", "la", "las", "el", "los", "un", "una", "unos", "unas", "y", "o", "e", "u",
        "en", "con", "sin", "por", "para", "al", "a", "x", "se", "su", "sus", "que", "mm",
        "und", "unid", "unidad", "unidades", "pza", "pieza", "piezas", "servicio", "servicios",
    }
)  # fmt: skip

_TOKEN = re.compile(r"[a-z0-9]+")
_CONTROL = re.compile(r"[\x00-\x1f\x7f]+")


def _tokens(text: Optional[str]) -> frozenset:
    """Palabras significativas de un texto: minúsculas, sin tildes, sin palabras vacías."""
    if not text:
        return frozenset()
    plano = unicodedata.normalize("NFKD", str(text).lower())
    plano = "".join(c for c in plano if not unicodedata.combining(c))
    return frozenset(
        t
        for t in _TOKEN.findall(plano)
        if t not in _STOPWORDS and (len(t) >= 3 or t.isdigit())
    )


def _dice(a: frozenset, b: frozenset) -> float:
    """Coeficiente de Dice entre dos conjuntos de palabras (0 sin nada en común, 1 iguales)."""
    if not a or not b:
        return 0.0
    return 2 * len(a & b) / (len(a) + len(b))


def debe_buscar_productos(modo: Optional[str], catalogo: Optional[list]) -> bool:
    """False si el tercero está forzado a cuenta o no hay productos que ofrecer.

    `account` gana siempre: el contador lo fijó y la detección de productos se ignora por
    completo, aunque la línea parezca un artículo inventariable.
    """
    if modo == MODE_ACCOUNT:
        return False
    return bool(catalogo)


def candidatos_por_linea(
    descripcion: Optional[str],
    catalogo: Iterable[dict],
    historial: Optional[Iterable[dict]] = None,
    k: int = DEFAULT_TOP_K,
) -> list[dict]:
    """Los `k` productos del catálogo que más se parecen a la descripción de una línea.

    - `catalogo`: filas con `code` y `description`.
    - `historial`: productos con que ya se contabilizaron facturas del tercero, cada uno con
      `code` y `descriptions` (cómo aparecieron en sus líneas). Sirve de dos formas: sus
      descripciones también se comparan, porque el proveedor llama a sus artículos a su manera
      y no como el catálogo, y su código recibe una bonificación.

    Devuelve `[{"code", "description"}]` ordenado de más a menos parecido; vacío si ninguna
    palabra significativa coincide. Orden estable: ante empate manda el código.
    """
    linea = _tokens(descripcion)
    if not linea:
        return []

    por_codigo_historial: dict[str, list[frozenset]] = {}
    for h in historial or []:
        code = str(h.get("code") or "").strip()
        if code:
            por_codigo_historial[code] = [_tokens(d) for d in h.get("descriptions") or []]

    puntuados = []
    for producto in catalogo:
        code = str(producto.get("code") or "").strip()
        if not code:
            continue
        propios = _tokens(producto.get("description"))
        textos = [propios, *por_codigo_historial.get(code, [])]
        base = max((_dice(linea, t) for t in textos if linea & t), default=0.0)
        if base <= 0.0:
            continue
        bonus = HISTORY_BONUS if code in por_codigo_historial else 0.0
        puntuados.append((base + bonus, code, producto))

    puntuados.sort(key=lambda p: (-p[0], p[1]))
    return [
        {"code": code, "description": _limpiar(producto.get("description"))}
        for _, code, producto in puntuados[: max(0, k)]
    ]


def resolver_producto(codigo: Optional[str], candidatos: Iterable[dict]) -> Optional[str]:
    """El código elegido por el modelo si es uno de los candidatos de la línea; si no, `None`.

    `None` significa «esta línea va por cuenta»: el modelo dijo que no es un producto, o
    devolvió algo que no se le ofreció.
    """
    if codigo is None:
        return None
    elegido = str(codigo).strip()
    if not elegido:
        return None
    permitidos = {str(c.get("code") or "").strip() for c in candidatos}
    return elegido if elegido in permitidos else None


def _limpiar(texto: Optional[str], largo: int = 120) -> str:
    """Descripción apta para el prompt: sin saltos ni control y acotada.

    El catálogo lo importa el cliente desde su propio archivo: un texto con saltos de línea
    podría simular instrucciones dentro del prompt.
    """
    return _CONTROL.sub(" ", str(texto or "")).strip()[:largo]
