import pytest
from app.application.dto.documents import DocumentsRangeRequest
from app.application.use_cases.fetch_and_enqueue_documents import FetchAndEnqueueDocumentsUseCase
from app.domain.exceptions.base import NoDocumentsFoundException


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


def _use_case(body):
    queue, store, progress = FakeQueue(), FakeBatchStore(), FakeProgress()
    uc = FetchAndEnqueueDocumentsUseCase(
        external_client=FakeClient(body),
        job_queue=queue,
        base_url="https://portal.example.com",
        login_url="https://portal.example.com/login",
        batch_store=store,
        job_progress_store=progress,
    )
    return uc, queue, store, progress


def _request():
    return DocumentsRangeRequest(token="t", StartDate="2026-10-05", EndDate="2026-10-06")


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
