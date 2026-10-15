import httpx
import pytest
from app.infrastructure.clients.processed_documents_client import HttpProcessedDocumentsClient


def _client_with(handler):
    client = HttpProcessedDocumentsClient(base_url="http://xml-processor:8001")
    transport = httpx.MockTransport(handler)
    original = httpx.AsyncClient
    return client, lambda **kw: original(transport=transport, **kw)


@pytest.mark.asyncio
async def test_envia_secreto_y_tenant_y_normaliza_a_minusculas(monkeypatch):
    monkeypatch.setenv("INTERNAL_SECRET", "s3cret")
    visto = {}

    def handler(request: httpx.Request) -> httpx.Response:
        visto["headers"] = request.headers
        visto["path"] = request.url.path
        return httpx.Response(200, json={"existing": ["ABC", "def"]})

    client, factory = _client_with(handler)
    monkeypatch.setattr(httpx, "AsyncClient", factory)

    result = await client.existing_track_ids("ikbo", ["abc", "def", "ghi"])

    assert result == {"abc", "def"}
    assert visto["path"] == "/internal/documents/existence-checks"
    assert visto["headers"]["x-internal-secret"] == "s3cret"
    assert visto["headers"]["x-tenant-slug"] == "ikbo"


@pytest.mark.asyncio
async def test_un_error_http_devuelve_vacio_sin_lanzar(monkeypatch):
    client, factory = _client_with(lambda r: httpx.Response(500))
    monkeypatch.setattr(httpx, "AsyncClient", factory)

    assert await client.existing_track_ids("ikbo", ["abc"]) == set()
