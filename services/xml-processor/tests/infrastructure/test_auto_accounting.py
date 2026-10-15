"""
Causación automática de documentos descargados de la DIAN (paso 8).

Dos cosas se prueban. La primera es la política: solo se dispara con historial confirmado por
el contador, y un duplicado que ya tiene cuentas no vuelve a gastar una llamada al modelo. La
segunda es el canal: el worker de descargas no tiene JWT de usuario, así que el cliente debe ir
por las rutas internas con `X-Internal-Secret` y `X-Tenant-Slug`. La versión anterior llamaba
al endpoint público sin credenciales y el llm-service respondía 401 a cada documento.
"""

from types import SimpleNamespace

import httpx
import pytest
from app.application.use_cases.auto_accounting import AutoAccountingUseCase
from app.infrastructure.clients.llm_client import LlmClient
from app.infrastructure.queue.download_queue import build_llm_client


class _Repo:
    def __init__(self, history=True, codes=()):
        self._history = history
        self._codes = codes

    def has_confirmed_accounting_history(self):
        return self._history

    def get_by_id(self, document_id):
        return SimpleNamespace(details=[SimpleNamespace(code=c) for c in self._codes])


class _Llm:
    def __init__(self, status=("triggered", None)):
        self.status = status
        self.assignments = []
        self.retentions = []

    async def trigger_code_assignment(self, document_id):
        self.assignments.append(document_id)
        return self.status

    async def trigger_retention_suggestion(self, document_id):
        self.retentions.append(document_id)


class TestPolitica:
    @pytest.mark.asyncio
    async def test_con_historial_asigna_cuentas_y_sugiere_retenciones(self):
        llm = _Llm()

        result = await AutoAccountingUseCase(_Repo(history=True), llm).execute(7)

        assert result == ("triggered", None)
        assert llm.assignments == [7] and llm.retentions == [7]

    @pytest.mark.asyncio
    async def test_sin_historial_no_llama_al_llm(self):
        llm = _Llm()

        result = await AutoAccountingUseCase(_Repo(history=False), llm).execute(7)

        assert result == (None, None)
        assert llm.assignments == [] and llm.retentions == []

    @pytest.mark.asyncio
    async def test_sin_cliente_no_hace_nada(self):
        assert await AutoAccountingUseCase(_Repo(), None).execute(7) == (None, None)

    @pytest.mark.asyncio
    async def test_un_error_del_llm_se_informa_y_no_impide_las_retenciones(self):
        llm = _Llm(status=("error", "HTTP 409: No tienes un plan único de cuenta"))

        result = await AutoAccountingUseCase(_Repo(), llm).execute(7)

        assert result == ("error", "HTTP 409: No tienes un plan único de cuenta")
        assert llm.retentions == [7]

    @pytest.mark.asyncio
    async def test_un_duplicado_con_cuentas_no_vuelve_a_causarse(self):
        llm = _Llm()
        repo = _Repo(codes=("510505", None))

        result = await AutoAccountingUseCase(repo, llm).execute(7, only_if_uncoded=True)

        assert result == (None, None) and llm.assignments == []

    @pytest.mark.asyncio
    async def test_un_duplicado_sin_cuentas_si_se_causa(self):
        llm = _Llm()

        result = await AutoAccountingUseCase(_Repo(codes=(None, "")), llm).execute(
            7, only_if_uncoded=True
        )

        assert result == ("triggered", None) and llm.assignments == [7]


class _FakeResponse:
    def __init__(self, status_code, payload=None, text=""):
        self.status_code = status_code
        self._payload = payload or {}
        self.text = text

    def json(self):
        return self._payload


class _FakeAsyncClient:
    calls: list = []
    response = _FakeResponse(200, {"assigned": 3, "skipped": 0})
    raises = None

    def __init__(self, *args, **kwargs):
        type(self).kwargs = kwargs

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return False

    async def post(self, url, headers=None, params=None):
        type(self).calls.append({"url": url, "headers": headers, "params": params})
        if type(self).raises is not None:
            raise type(self).raises
        return type(self).response


@pytest.fixture
def fake_http(monkeypatch):
    _FakeAsyncClient.calls = []
    _FakeAsyncClient.response = _FakeResponse(200, {"assigned": 3, "skipped": 0})
    _FakeAsyncClient.raises = None
    monkeypatch.setattr(httpx, "AsyncClient", _FakeAsyncClient)
    return _FakeAsyncClient


@pytest.fixture
def internal_client():
    return LlmClient(base_url="http://llm-service:8003", internal_secret="s3", tenant_slug="ikbo")


class TestCanalInterno:
    @pytest.mark.asyncio
    async def test_las_cuentas_van_por_la_ruta_interna_con_secreto_y_tenant(
        self, internal_client, fake_http
    ):
        await internal_client.trigger_code_assignment(42)

        call = fake_http.calls[0]
        assert call["url"] == "http://llm-service:8003/internal/accounting/code-assignments/42"
        assert call["headers"] == {"X-Internal-Secret": "s3", "X-Tenant-Slug": "ikbo"}
        assert "Authorization" not in call["headers"]

    @pytest.mark.asyncio
    async def test_las_retenciones_van_por_la_ruta_interna_y_se_persisten(
        self, internal_client, fake_http
    ):
        await internal_client.trigger_retention_suggestion(42)

        call = fake_http.calls[0]
        assert call["url"] == (
            "http://llm-service:8003/internal/accounting/retention-suggestions/42"
        )
        assert call["params"] == {"persist": "true"}
        assert call["headers"]["X-Internal-Secret"] == "s3"

    @pytest.mark.asyncio
    async def test_con_token_sigue_usando_la_ruta_publica(self, fake_http):
        client = LlmClient(base_url="http://llm-service:8003", bearer_token="tok")

        await client.trigger_code_assignment(42)

        call = fake_http.calls[0]
        assert call["url"] == "http://llm-service:8003/api/v1/accounting/code-assignments/42"
        assert call["headers"] == {"Authorization": "Bearer tok"}


class TestResultado:
    @pytest.mark.asyncio
    async def test_un_200_es_triggered(self, internal_client, fake_http):
        assert await internal_client.trigger_code_assignment(1) == ("triggered", None)

    @pytest.mark.asyncio
    async def test_un_http_de_error_informa_el_motivo(self, internal_client, fake_http):
        fake_http.response = _FakeResponse(401, text='{"detail":"Not authenticated"}')

        status, error = await internal_client.trigger_code_assignment(1)

        assert status == "error" and error.startswith("HTTP 401")

    @pytest.mark.asyncio
    async def test_un_fallo_de_red_se_informa_sin_lanzar(self, internal_client, fake_http):
        fake_http.raises = httpx.ConnectError("sin ruta al host")

        status, error = await internal_client.trigger_code_assignment(1)

        assert status == "error" and "sin ruta" in error

    @pytest.mark.asyncio
    async def test_el_tiempo_de_espera_supera_al_del_modelo(self, internal_client, fake_http):
        """OPENAI_TIMEOUT_SECONDS es 60: con 30 s se rendía antes que el modelo."""
        await internal_client.trigger_code_assignment(1)

        assert fake_http.kwargs["timeout"] > 60


class TestConstruccion:
    def test_el_worker_arma_el_cliente_interno_con_el_tenant(self, monkeypatch):
        monkeypatch.setenv("INTERNAL_SECRET", "s3")

        client = build_llm_client("ikbo")

        assert client._headers == {"X-Internal-Secret": "s3", "X-Tenant-Slug": "ikbo"}

    def test_sin_tenant_no_hay_cliente(self):
        assert build_llm_client("") is None
