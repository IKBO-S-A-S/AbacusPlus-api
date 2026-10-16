"""Estado de un lote: cuándo se da por terminado.

Un lote sin documentos —porque todos ya estaban guardados o ninguno era descargable— nunca
llegaba a `is_done`: la condición exigía `total > 0`, y la interfaz lo consultaba sin fin.
"""

from datetime import datetime, timezone

import pytest
from app.application.use_cases.get_batch_status import GetBatchStatusUseCase


class _Lotes:
    def __init__(self, data):
        self._data = data

    async def get(self, batch_id):
        return self._data


class _Progreso:
    def __init__(self, por_job=None):
        self._por_job = por_job or {}

    async def get(self, job_id):
        return self._por_job.get(job_id)


def _lote(job_ids):
    return {
        "started_at": datetime.now(timezone.utc).isoformat(),
        "total": len(job_ids),
        "job_ids": job_ids,
        "job_track_map": {j: j for j in job_ids},
    }


def _terminado(**extra):
    return {
        "downloaded_done": "1",
        "download_status": "ok",
        "xml_done": "1",
        "xml_status": "added",
        "accounting_done": "1",
        "accounting_status": "skipped",
        **extra,
    }


@pytest.mark.asyncio
async def test_un_lote_vacio_se_da_por_terminado():
    uc = GetBatchStatusUseCase(_Lotes(_lote([])), _Progreso())

    result = await uc.execute("b1")

    assert result.total == 0
    assert result.is_done is True
    assert result.total_time_seconds is not None
    assert result.auth_error is None


@pytest.mark.asyncio
async def test_un_lote_con_trabajo_pendiente_no_esta_terminado():
    uc = GetBatchStatusUseCase(_Lotes(_lote(["a", "b"])), _Progreso({"a": _terminado()}))

    result = await uc.execute("b1")

    assert result.is_done is False and result.total_time_seconds is None


@pytest.mark.asyncio
async def test_un_lote_con_todos_sus_documentos_listos_esta_terminado():
    uc = GetBatchStatusUseCase(
        _Lotes(_lote(["a", "b"])), _Progreso({"a": _terminado(), "b": _terminado()})
    )

    result = await uc.execute("b1")

    assert result.is_done is True and result.total_time_seconds is not None


@pytest.mark.asyncio
async def test_un_lote_inexistente_sigue_devolviendo_none():
    assert await GetBatchStatusUseCase(_Lotes(None), _Progreso()).execute("x") is None
