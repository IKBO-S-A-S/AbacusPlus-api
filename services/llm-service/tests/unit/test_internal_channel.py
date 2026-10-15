"""Causación sin usuario: el canal servicio-a-servicio del llm-service.

La causación de un documento descargado de la DIAN la dispara un worker, sin JWT. Para que el
llm-service pueda trabajar así, cada cliente que antes llevaba el token del usuario tiene un
modo interno (`X-Internal-Secret` + `X-Tenant-Slug`, rutas `/internal/*`), y el servicio
expone las dos operaciones bajo `/internal/accounting/*`. Se usan los clientes **reales** y se
sustituye solo el transporte HTTP, por la misma razón que en `test_rag_client_contract`.
"""

import pytest
from app.infrastructure.clients import catalog_client as catalog_module
from app.infrastructure.clients import integration_config_client as config_module
from app.infrastructure.clients import rag_client as rag_module
from app.infrastructure.clients.catalog_client import CatalogClient
from app.infrastructure.clients.integration_config_client import IntegrationConfigClient
from app.infrastructure.clients.rag_client import RagClient

INTERNO = {"X-Internal-Secret": "s3", "X-Tenant-Slug": "ikbo"}


class _Respuesta:
    def __init__(self, payload):
        self._payload = payload

    def raise_for_status(self):
        return None

    def json(self):
        return self._payload


class _Transporte:
    def __init__(self, payload):
        self._payload = payload
        self.peticiones: list[dict] = []

    async def get(self, url, params=None, headers=None, timeout=None):
        self.peticiones.append({"url": url, "params": params, "headers": headers})
        return _Respuesta(self._payload)

    async def post(self, url, json=None, headers=None, timeout=None):
        self.peticiones.append({"url": url, "json": json, "headers": headers})
        return _Respuesta(self._payload)


def _usar(monkeypatch, modulo, payload):
    transporte = _Transporte(payload)

    async def _get_client():
        return transporte

    monkeypatch.setattr(modulo, "get_client", _get_client)
    return transporte


def _config_interno(tenant="ikbo"):
    # Tenant distinto por prueba: el catálogo se cachea por empresa y entre pruebas se pisaría.
    return IntegrationConfigClient(
        base_url="http://config:8007", tenant_slug=tenant, internal_secret="s3"
    )


class TestIntegrationConfigInterno:
    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "metodo, ruta_interna",
        [
            ("get_chart_accounts", "/internal/chart-accounts"),
            ("get_cost_centers", "/internal/cost-centers"),
            ("get_retentions", "/internal/retentions"),
        ],
    )
    async def test_cada_catalogo_va_por_su_ruta_interna(self, monkeypatch, metodo, ruta_interna):
        t = _usar(monkeypatch, config_module, [])

        await getattr(_config_interno(f"t-{metodo}"), metodo)()

        assert t.peticiones[0]["url"] == f"http://config:8007{ruta_interna}"
        assert t.peticiones[0]["headers"] == {
            "X-Internal-Secret": "s3",
            "X-Tenant-Slug": f"t-{metodo}",
        }

    @pytest.mark.asyncio
    async def test_perfil_fiscal_y_criterios_van_por_su_ruta_interna(self, monkeypatch):
        t = _usar(monkeypatch, config_module, {"criterios": []})
        cliente = _config_interno("t-perfil")

        await cliente.get_fiscal_profile()
        await cliente.get_retention_criteria()

        urls = [p["url"] for p in t.peticiones]
        assert urls == [
            "http://config:8007/internal/fiscal-profile",
            "http://config:8007/internal/retention-criteria",
        ]

    @pytest.mark.asyncio
    async def test_los_impuestos_piden_el_catalogo_completo(self, monkeypatch):
        """La ruta pública devuelve todos; la interna filtra por activo salvo que se le pida."""
        t = _usar(monkeypatch, config_module, [])

        await _config_interno("t-impuestos").get_taxes()

        assert t.peticiones[0]["url"] == "http://config:8007/internal/taxes"
        assert t.peticiones[0]["params"] == {"include_inactive": "true"}

    @pytest.mark.asyncio
    async def test_con_token_sigue_usando_las_rutas_publicas(self, monkeypatch):
        t = _usar(monkeypatch, config_module, [])
        cliente = IntegrationConfigClient(
            base_url="http://config:8007", bearer_token="tok", tenant_slug="t-publico"
        )

        await cliente.get_retentions()

        assert t.peticiones[0]["url"] == "http://config:8007/api/v1/integrations/retentions"
        assert t.peticiones[0]["headers"] == {"Authorization": "Bearer tok"}


class TestRagInterno:
    @pytest.mark.asyncio
    async def test_la_busqueda_va_por_la_ruta_interna_con_el_tenant(self, monkeypatch):
        t = _usar(monkeypatch, rag_module, {"results": [{"source_id": 1}]})
        cliente = RagClient(base_url="http://rag:8002", internal_secret="s3", tenant_slug="ikbo")

        resultados = await cliente.search("q", only_validated=True)

        assert resultados == [{"source_id": 1}]
        assert t.peticiones[0]["url"] == "http://rag:8002/internal/chunks/search"
        assert t.peticiones[0]["headers"] == INTERNO
        assert t.peticiones[0]["json"]["only_validated"] is True

    @pytest.mark.asyncio
    async def test_con_token_sigue_usando_la_ruta_publica(self, monkeypatch):
        t = _usar(monkeypatch, rag_module, {"results": []})

        await RagClient(base_url="http://rag:8002", bearer_token="tok").search("q")

        assert t.peticiones[0]["url"] == "http://rag:8002/api/v1/chunks/search"
        assert t.peticiones[0]["headers"] == {"Authorization": "Bearer tok"}


class TestCatalogoInterno:
    @pytest.mark.asyncio
    async def test_las_tarifas_de_retefuente_van_por_la_ruta_interna(self, monkeypatch):
        t = _usar(monkeypatch, catalog_module, [])
        cliente = CatalogClient(base_url="http://xml:8001", internal_secret="s3", tenant_slug="ikbo")

        await cliente.get_retention_fuente_rates()

        assert t.peticiones[0]["url"] == "http://xml:8001/internal/catalog/retention-fuente-rates"
        assert t.peticiones[0]["headers"] == INTERNO

    @pytest.mark.asyncio
    async def test_con_token_sigue_usando_la_ruta_publica(self, monkeypatch):
        t = _usar(monkeypatch, catalog_module, [])

        await CatalogClient(base_url="http://xml:8001", bearer_token="tok").get_retention_fuente_rates()

        assert t.peticiones[0]["url"] == "http://xml:8001/api/v1/catalog/retention-fuente-rates"


class TestRutasInternas:
    """Las dos operaciones exigen el secreto interno y no aceptan un JWT en su lugar."""

    @pytest.fixture
    def cliente(self, monkeypatch):
        monkeypatch.setenv("INTERNAL_SECRET", "s3")
        from app.dependencies import (
            get_internal_assign_account_codes_use_case,
            get_internal_suggest_retentions_use_case,
        )
        from app.main import app
        from fastapi.testclient import TestClient

        class _Casos:
            def __init__(self):
                self.llamadas = []

            async def execute(self, document_id, **kwargs):
                self.llamadas.append((document_id, kwargs))
                if document_id == 404:
                    raise ValueError("Documento 404 no encontrado")
                return {
                    "document_id": document_id,
                    "assigned": 2,
                    "skipped": 0,
                    "warnings": [],
                    "suggestions": [],
                    "persisted": None,
                }

        casos = _Casos()
        app.dependency_overrides[get_internal_assign_account_codes_use_case] = lambda: casos
        app.dependency_overrides[get_internal_suggest_retentions_use_case] = lambda: casos
        try:
            yield TestClient(app), casos
        finally:
            app.dependency_overrides.clear()

    def test_sin_secreto_se_rechaza(self, cliente):
        http, casos = cliente

        r = http.post("/internal/accounting/code-assignments/1", headers={"X-Tenant-Slug": "ikbo"})

        assert r.status_code in (403, 422) and casos.llamadas == []

    def test_con_secreto_equivocado_se_rechaza(self, cliente):
        http, casos = cliente

        r = http.post(
            "/internal/accounting/code-assignments/1",
            headers={"X-Internal-Secret": "otro", "X-Tenant-Slug": "ikbo"},
        )

        assert r.status_code == 403 and casos.llamadas == []

    def test_un_jwt_no_sustituye_al_secreto(self, cliente):
        http, casos = cliente

        r = http.post(
            "/internal/accounting/retention-suggestions/1",
            headers={"Authorization": "Bearer algo", "X-Tenant-Slug": "ikbo"},
        )

        assert r.status_code in (403, 422) and casos.llamadas == []

    def test_con_secreto_asigna_cuentas(self, cliente):
        http, casos = cliente

        r = http.post("/internal/accounting/code-assignments/5", headers=INTERNO)

        assert r.status_code == 200 and r.json()["assigned"] == 2
        assert casos.llamadas == [(5, {"overwrite_manual": False})]

    def test_documento_inexistente_es_404(self, cliente):
        http, _ = cliente

        r = http.post("/internal/accounting/code-assignments/404", headers=INTERNO)

        assert r.status_code == 404

    def test_las_retenciones_aceptan_persist(self, cliente):
        http, casos = cliente

        r = http.post(
            "/internal/accounting/retention-suggestions/5?persist=true", headers=INTERNO
        )

        assert r.status_code == 200
        assert casos.llamadas == [(5, {"overwrite_manual": False, "persist": True})]
