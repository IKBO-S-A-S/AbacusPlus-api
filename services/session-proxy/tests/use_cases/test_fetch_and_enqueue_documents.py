import pytest
from app.application.dto.documents import DocumentsRangeRequest
from app.application.use_cases.fetch_and_enqueue_documents import FetchAndEnqueueDocumentsUseCase
from app.domain.exceptions.base import (
    FiscalNitNotConfiguredException,
    NoDocumentsFoundException,
    RkNitMismatchException,
)


class FakeClient:
    def __init__(self, body):
        self._body = body

    async def login_and_request(self, **kwargs):
        return {"status_code": 200, "body": self._body}


class FakeQueue:
    def __init__(self):
        self.enqueued = []

    async def enqueue(self, function, **kwargs):
        self.enqueued.append((function, kwargs))
        return "job-1"


class FakeBatchStore:
    def __init__(self):
        self.saved = []

    async def save(self, *args):
        self.saved.append(args)


class FakeProgress:
    def __init__(self):
        self.inited = []

    async def init(self, job_id, track_id):
        self.inited.append(track_id)


class FakeFiscalProfile:
    def __init__(self, nit):
        self._nit = nit

    async def get_nit(self, tenant_slug):
        return self._nit


def _use_case(body, nit="900031352"):
    queue, store, progress = FakeQueue(), FakeBatchStore(), FakeProgress()
    uc = FetchAndEnqueueDocumentsUseCase(
        external_client=FakeClient(body),
        job_queue=queue,
        base_url="https://portal.example.com",
        login_url="https://portal.example.com/login",
        batch_store=store,
        job_progress_store=progress,
        fiscal_profile=FakeFiscalProfile(nit),
    )
    return uc, queue, store, progress


def _request(rk="900031352"):
    return DocumentsRangeRequest(
        token="t", rk=rk, StartDate="2026-10-05", EndDate="2026-10-06"
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("body", [{"data": []}, [], {}])
async def test_sin_documentos_lanza_error_y_no_encola(body):
    uc, queue, store, progress = _use_case(body)

    with pytest.raises(NoDocumentsFoundException) as exc:
        await uc.execute(_request(), "ikbo")

    assert "2026-10-05" in exc.value.message and "2026-10-06" in exc.value.message
    assert queue.enqueued == []
    assert store.saved == []
    assert progress.inited == []


@pytest.mark.asyncio
async def test_con_documentos_encola_el_lote():
    uc, queue, store, _ = _use_case({"data": [{"Id": "abc", "DocumentTypeId": "01"}]})

    result = await uc.execute(_request(), "ikbo")

    assert result.enqueued == 1
    assert len(queue.enqueued) == 1
    assert len(store.saved) == 1


@pytest.mark.asyncio
async def test_rk_distinto_al_nit_del_perfil_se_rechaza_sin_tocar_la_dian():
    uc, queue, store, _ = _use_case({"data": [{"Id": "abc", "DocumentTypeId": "01"}]})

    with pytest.raises(RkNitMismatchException) as exc:
        await uc.execute(_request(rk="811000111"), "ikbo")

    assert "no corresponde al NIT registrado" in exc.value.message
    assert queue.enqueued == [] and store.saved == []


@pytest.mark.asyncio
async def test_rk_vacio_sin_fallback_se_rechaza(monkeypatch):
    monkeypatch.delenv("EXTERNAL_FIXED_RK", raising=False)
    uc, _, _, _ = _use_case({"data": []})

    with pytest.raises(RkNitMismatchException):
        await uc.execute(_request(rk=""), "ikbo")


@pytest.mark.asyncio
async def test_rk_con_formato_se_compara_por_digitos():
    uc, queue, _, _ = _use_case({"data": [{"Id": "abc", "DocumentTypeId": "01"}]})

    await uc.execute(_request(rk="900.031.352"), "ikbo")

    assert len(queue.enqueued) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("nit", [None, ""])
async def test_perfil_sin_nit_se_rechaza(nit):
    uc, queue, _, _ = _use_case({"data": [{"Id": "abc", "DocumentTypeId": "01"}]}, nit=nit)

    with pytest.raises(FiscalNitNotConfiguredException):
        await uc.execute(_request(), "ikbo")

    assert queue.enqueued == []
