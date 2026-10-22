"""Fórmulas puras de los indicadores del dashboard.

Nada aquí toca la base de datos: son las reglas de negocio de los indicadores, separadas de
la consulta que las alimenta para poder probarlas con casos borde sin montar datos.

Convenciones que se aplican a TODOS los indicadores:

- **Sin base no hay porcentaje.** Una división cuyo denominador es cero devuelve `None`, nunca
  `0`: «0 % de error» y «no hubo documentos» son afirmaciones distintas y el frontend debe
  poder distinguirlas.
- **Periodo anterior comparable** = la ventana inmediatamente anterior, de la misma duración.
- **Variación** = (actual − anterior) / |anterior| × 100. Si el anterior es 0 o no existe, la
  variación es `None` (no hay base comparable), no infinito.
"""

from __future__ import annotations

from datetime import date, timedelta
from typing import Optional

# ── Sentido del indicador ───────────────────────────────────────────────────────────────
#: Un valor más alto es mejor (p. ej. tasa de contabilización).
HIGHER_IS_BETTER = "higher_is_better"
#: Un valor más bajo es mejor (p. ej. tasa de error, backlog).
LOWER_IS_BETTER = "lower_is_better"
#: Informativo: no tiene un «bueno» o «malo» inherente (p. ej. gasto total). No lleva semáforo.
NEUTRAL = "neutral"

GREEN = "green"
YELLOW = "yellow"
RED = "red"

#: Rangos de antigüedad del backlog, en días. El último es abierto («15+» = más de 15 días).
AGE_BUCKETS: tuple[tuple[str, int, Optional[int]], ...] = (
    ("0-3", 0, 3),
    ("4-7", 4, 7),
    ("8-15", 8, 15),
    ("15+", 16, None),
)

GRANULARITIES = ("day", "week", "month")

#: Por encima de esto la ventana es casi seguro un error del cliente y barrerla es caro.
MAX_PERIOD_DAYS = 731


def safe_ratio_pct(numerator: Optional[float], denominator: Optional[float]) -> Optional[float]:
    """Porcentaje con dos decimales, o `None` si no hay denominador."""
    if numerator is None or not denominator:
        return None
    return round(float(numerator) / float(denominator) * 100, 2)


def variation_pct(current: Optional[float], previous: Optional[float]) -> Optional[float]:
    """Variación porcentual contra el periodo anterior; `None` si no hay base comparable."""
    if current is None or previous is None or previous == 0:
        return None
    return round((float(current) - float(previous)) / abs(float(previous)) * 100, 2)


def previous_period(start: date, end: date) -> tuple[date, date]:
    """Ventana inmediatamente anterior de la misma duración (ambos extremos incluidos)."""
    days = (end - start).days + 1
    prev_end = start - timedelta(days=1)
    prev_start = prev_end - timedelta(days=days - 1)
    return prev_start, prev_end


def traffic_light(
    value: Optional[float],
    direction: str,
    target: Optional[float],
    warning: Optional[float] = None,
) -> Optional[str]:
    """Semáforo de un indicador frente a su meta.

    - Sin valor, sin meta o sentido neutro → `None` (no se inventa un color).
    - `higher_is_better`: verde si `valor >= meta`; amarillo si `valor >= alerta`; rojo en otro caso.
    - `lower_is_better`: verde si `valor <= meta`; amarillo si `valor <= alerta`; rojo en otro caso.
    - Sin `alerta` no hay banda amarilla: es verde o rojo.
    """
    if value is None or target is None or direction == NEUTRAL:
        return None
    if direction == HIGHER_IS_BETTER:
        if value >= target:
            return GREEN
        if warning is not None and value >= warning:
            return YELLOW
        return RED
    if direction == LOWER_IS_BETTER:
        if value <= target:
            return GREEN
        if warning is not None and value <= warning:
            return YELLOW
        return RED
    return None


def age_bucket_label(age_days: int) -> str:
    """Rango de antigüedad al que pertenece una edad en días (negativas se tratan como 0)."""
    age = max(age_days, 0)
    for label, low, high in AGE_BUCKETS:
        if age >= low and (high is None or age <= high):
            return label
    return AGE_BUCKETS[-1][0]  # pragma: no cover - el último rango es abierto


def age_bucket_date_range(label: str, today: date) -> tuple[Optional[date], Optional[date]]:
    """Rango de FECHAS de emisión `(desde, hasta)` que cae en un rango de antigüedad.

    Antigüedad = `today − date`. Se traduce a fechas para que la base compare contra la
    columna indexada en lugar de calcular una resta por fila. Los extremos son inclusivos y
    `None` significa «sin límite». Los documentos con fecha futura se cuentan en el primer
    rango, por eso su `hasta` no tiene tope.
    """
    for name, low, high in AGE_BUCKETS:
        if name != label:
            continue
        newest = None if low == 0 else today - timedelta(days=low)
        oldest = None if high is None else today - timedelta(days=high)
        # `newest` es la fecha más reciente del rango (menor antigüedad).
        return oldest, newest
    raise ValueError(f"Rango de antigüedad desconocido: {label}")


def bucket_start(day: date, granularity: str) -> date:
    """Primer día del bucket temporal que contiene `day` (week: lunes)."""
    if granularity == "day":
        return day
    if granularity == "week":
        return day - timedelta(days=day.weekday())
    if granularity == "month":
        return day.replace(day=1)
    raise ValueError(f"Granularidad desconocida: {granularity}")


def bucket_end(start: date, granularity: str) -> date:
    """Último día (inclusive) del bucket que empieza en `start`."""
    if granularity == "day":
        return start
    if granularity == "week":
        return start + timedelta(days=6)
    if granularity == "month":
        nxt = (start.replace(day=28) + timedelta(days=4)).replace(day=1)
        return nxt - timedelta(days=1)
    raise ValueError(f"Granularidad desconocida: {granularity}")


def trend(values: list[float], tolerance_pct: float = 1.0) -> Optional[str]:
    """Tendencia entre los dos últimos valores: `up`, `down` o `flat`.

    Se espera que `values` ya excluya el bucket en curso (incompleto), que haría parecer una
    caída lo que es solo un periodo sin terminar. Con menos de dos valores, o si el anterior
    es 0, no hay base y devuelve `None`. Una variación dentro de `tolerance_pct` es `flat`.
    """
    if len(values) < 2:
        return None
    variation = variation_pct(values[-1], values[-2])
    if variation is None:
        return None
    if abs(variation) <= tolerance_pct:
        return "flat"
    return "up" if variation > 0 else "down"
