"""Parámetros configurables del dashboard de indicadores.

Las metas y los umbrales de semáforo no viven en el código: cambiarlos es editar una variable
de entorno y reiniciar. Igual que en `accounting_settings`, un valor mal escrito no tumba el
servicio: se registra y ese indicador queda sin meta (sin semáforo), que es el modo seguro.

`DASHBOARD_TARGETS` es un JSON `{codigo_kpi: {"meta": n, "alerta": n}}`; `alerta` es opcional
y delimita la banda amarilla. Ejemplo:

    DASHBOARD_TARGETS={"tasa_contabilizacion":{"meta":90,"alerta":75},"tasa_error":{"meta":2,"alerta":5}}

`DASHBOARD_TIMEZONE` es la zona con la que se decide a qué mes pertenece un instante
(`accounted_at` está en UTC; sin convertir, una contabilización de las 8 p. m. del último día
del mes en Colombia caería en el mes siguiente).
"""

import json
import logging
import os
from dataclasses import dataclass
from typing import Optional

logger = logging.getLogger(__name__)

DEFAULT_TIMEZONE = "America/Bogota"


@dataclass(frozen=True)
class Target:
    meta: float
    alerta: Optional[float] = None


def _as_float(value) -> Optional[float]:
    if isinstance(value, bool):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def load_targets() -> dict[str, Target]:
    raw = os.getenv("DASHBOARD_TARGETS", "").strip()
    if not raw:
        return {}
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError:
        logger.warning("DASHBOARD_TARGETS no es JSON válido; los indicadores quedan sin meta")
        return {}
    if not isinstance(parsed, dict):
        logger.warning("DASHBOARD_TARGETS debe ser un objeto JSON; los indicadores quedan sin meta")
        return {}

    targets: dict[str, Target] = {}
    for code, cfg in parsed.items():
        meta = _as_float(cfg.get("meta")) if isinstance(cfg, dict) else None
        if meta is None:
            logger.warning("DASHBOARD_TARGETS['%s'] sin 'meta' numérica; se ignora", code)
            continue
        targets[str(code)] = Target(meta=meta, alerta=_as_float(cfg.get("alerta")))
    return targets


def get_timezone() -> str:
    return os.getenv("DASHBOARD_TIMEZONE", "").strip() or DEFAULT_TIMEZONE


def today_local():
    """Fecha de hoy en la zona del dashboard.

    «Hoy» decide la antigüedad del backlog y qué bucket de la serie sigue en curso; con la
    fecha UTC del servidor, entre las 7 p. m. y la medianoche en Colombia ya sería «mañana».
    Si la imagen no trae la base de zonas horarias se cae a la fecha del servidor, avisando.
    """
    from datetime import date, datetime
    from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

    try:
        return datetime.now(ZoneInfo(get_timezone())).date()
    except ZoneInfoNotFoundError:
        logger.warning(
            "Zona horaria '%s' no disponible (¿falta tzdata?); se usa la fecha del servidor",
            get_timezone(),
        )
        return date.today()
