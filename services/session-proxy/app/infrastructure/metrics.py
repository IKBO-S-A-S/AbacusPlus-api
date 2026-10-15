"""Métricas de tiempo por etapa del pipeline de descargas.

Cada medición sale como una línea `[METRIC] ...` con pares clave=valor, para poder filtrarla
con `grep "\\[METRIC\\]"` y comparar antes/después de cada optimización. No toca ninguna API
ni almacén: solo registra.
"""

import logging
import time
from contextlib import contextmanager
from typing import Optional

logger = logging.getLogger("metrics")


def _fmt(fields: dict) -> str:
    return " ".join(f"{k}={v}" for k, v in fields.items() if v is not None)


@contextmanager
def timed(stage: str, sink: Optional[dict] = None):
    """Mide el bloque y lo acumula en `sink[stage]` (ms). Acumula para sumar los reintentos."""
    start = time.perf_counter()
    try:
        yield
    finally:
        if sink is not None:
            sink[stage] = sink.get(stage, 0) + round((time.perf_counter() - start) * 1000)


def log_metric(event: str, **fields) -> None:
    logger.info("[METRIC] event=%s %s", event, _fmt(fields))
