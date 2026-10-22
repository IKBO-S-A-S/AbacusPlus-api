"""Dashboard de indicadores: agregaciones, comparación con el periodo anterior, drill-down y HTTP.

«Hoy» está fijado en 2026-10-22 (jueves). Con el periodo 2026-10-01..2026-10-22 el periodo
anterior comparable es 2026-09-09..2026-09-30.
"""

from datetime import date, datetime, timezone

import pytest
from app.application.dto.dashboard import DashboardFilters
from app.application.use_cases.dashboard import DashboardUseCase
from app.dependencies import get_dashboard_use_case
from app.infrastructure.config.dashboard_settings import Target, load_targets
from app.infrastructure.persistence.models.document import Document
from app.infrastructure.persistence.models.processing_log import ProcessingLog
from app.infrastructure.persistence.repositories.dashboard_repository import (
    DashboardRepository,
)
from app.main import app
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

TODAY = date(2026, 10, 22)
FROM, TO = date(2026, 10, 1), date(2026, 10, 22)


def _doc(n, day, status, total=1000.0, **kw):
    base = {
        "document_name": f"{n}.xml",
        "document_number": f"FE{n}",
        "date": day,
        "hour": "10:00",
        "currency": "COP",
        "document_type": "Factura",
        "uuid": f"u{n}",
        "issuer_name": "Proveedor",
        "issuer_nit": "900123456",
        "receiver_name": "Empresa",
        "receiver_nit": "800987654",
        "subtotal": total,
        "total_taxes": 0.0,
        "total": total,
        "status": status,
        "cost_center_id": None,
        "pdf_url": None,
    }
    base.update(kw)
    return Document(**base)


def _seed(db):
    # ── Periodo actual: 1..22 de octubre ───────────────────────────────────────────────
    db.add_all(
        [
            # contabilizado a tiempo (mismo mes), con soporte
            _doc(
                1,
                date(2026, 10, 20),
                400,
                1000,
                pdf_url="s3://a",
                accounted_at=datetime(2026, 10, 21, 12, tzinfo=timezone.utc),
            ),
            # contabilizado en un mes posterior, con soporte
            _doc(
                2,
                date(2026, 10, 2),
                400,
                2000,
                pdf_url="s3://b",
                accounted_at=datetime(2026, 11, 2, 12, tzinfo=timezone.utc),
            ),
            # aprobado (7 días: rango 4-7), con soporte
            _doc(3, date(2026, 10, 15), 300, 3000, pdf_url="s3://c"),
            # aprobado (17 días: rango 15+), otro proveedor
            _doc(4, date(2026, 10, 5), 300, 4000, issuer_nit="800000001"),
            # causado = pendiente de aprobación
            _doc(5, date(2026, 10, 21), 200, 5000),
            # error de contabilización (12 días: rango 8-15)
            _doc(6, date(2026, 10, 10), 0, 6000, accounting_error="cuenta inválida"),
            # procesado de hoy
            _doc(7, date(2026, 10, 22), 100, 7000),
            # contabilizado sin accounted_at (cerrado por reconciliación)
            _doc(8, date(2026, 10, 19), 400, 8000, accounted_at=None),
            # ── Periodo anterior: 9..30 de septiembre ──────────────────────────────────
            _doc(
                9,
                date(2026, 9, 15),
                400,
                900,
                accounted_at=datetime(2026, 9, 20, 12, tzinfo=timezone.utc),
            ),
            _doc(10, date(2026, 9, 20), 100, 100),
        ]
    )
    db.add_all(
        [
            ProcessingLog(
                filename="a.zip", status="duplicate", processed_at=datetime(2026, 10, 10)
            ),
            ProcessingLog(
                filename="b.zip", status="duplicate", processed_at=datetime(2026, 10, 11)
            ),
            ProcessingLog(filename="c.zip", status="duplicate", processed_at=datetime(2026, 9, 12)),
            ProcessingLog(filename="d.zip", status="added", processed_at=datetime(2026, 10, 10)),
            # 22 de octubre a las 23:59 sigue dentro del periodo (el límite es inclusivo)
            ProcessingLog(
                filename="e.zip", status="duplicate", processed_at=datetime(2026, 10, 22, 23, 59)
            ),
        ]
    )
    db.commit()


@pytest.fixture
def db():
    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    from app.infrastructure.config.database import Base

    Base.metadata.create_all(engine)
    session = sessionmaker(bind=engine)()
    _seed(session)
    yield session
    session.close()
    Base.metadata.drop_all(engine)


def _uc(db, targets=None):
    return DashboardUseCase(
        DashboardRepository(db),
        targets=targets,
        today=lambda: TODAY,
        now=lambda: datetime(2026, 10, 22, 15, tzinfo=timezone.utc),
    )


def _filters(**kw):
    return DashboardFilters(
        from_date=kw.pop("from_date", FROM), to_date=kw.pop("to_date", TO), **kw
    )


def _kpis(resp):
    return {k.code: k for k in resp.kpis}


# ── Vista ejecutiva ─────────────────────────────────────────────────────────────────────


class TestExecutive:
    def test_valores_y_periodo_anterior(self, db):
        resp = _uc(db).executive(_filters())
        k = _kpis(resp)

        assert (resp.period.previous_from_date, resp.period.previous_to_date) == (
            date(2026, 9, 9),
            date(2026, 9, 30),
        )
        assert k["documents_total"].value == 8 and k["documents_total"].previous_value == 2
        assert k["documents_total"].variation_pct == 300.0
        assert k["amount_total"].value == 36000.0
        assert k["accounting_rate"].value == 37.5  # 3 de 8
        assert k["accounting_rate"].previous_value == 50.0  # 1 de 2
        assert k["accounting_rate"].variation_pct == -25.0
        assert k["error_rate"].value == 12.5
        assert k["pending_approval"].value == 1
        assert k["approved_not_accounted"].value == 2
        assert k["without_support"].value == 5

    def test_periodo_correcto_ignora_los_contabilizados_sin_fecha(self, db):
        """El doc 8 está contabilizado pero sin `accounted_at`: no entra ni al numerador ni al
        denominador (no se puede afirmar que fue a tiempo ni que fue tarde)."""
        k = _kpis(_uc(db).executive(_filters()))
        assert k["accounted_on_time_rate"].value == 50.0  # 1 de 2 con fecha
        assert k["accounted_on_time_rate"].previous_value == 100.0

    def test_duplicados_incluye_el_limite_superior_inclusivo(self, db):
        k = _kpis(_uc(db).executive(_filters()))
        assert k["duplicates"].value == 3  # 10, 11 y 22 (23:59) de octubre
        assert k["duplicates"].previous_value == 1

    def test_duplicados_no_aplica_con_filtros_de_documento(self, db):
        k = _kpis(_uc(db).executive(_filters(issuer_nit="900123456")))
        assert k["duplicates"].value is None
        assert "filtro" in k["duplicates"].note

    def test_periodo_vacio_no_inventa_porcentajes(self, db):
        k = _kpis(
            _uc(db).executive(_filters(from_date=date(2025, 1, 1), to_date=date(2025, 1, 31)))
        )
        assert k["documents_total"].value == 0
        assert k["amount_total"].value == 0.0
        for code in ("accounting_rate", "error_rate", "accounted_on_time_rate"):
            assert k[code].value is None and k[code].variation_pct is None
            assert k[code].light is None

    def test_semaforo_con_metas_configuradas(self, db):
        targets = {
            "accounting_rate": Target(meta=90, alerta=40),
            "error_rate": Target(meta=2, alerta=5),
            "documents_total": Target(meta=1),
        }
        k = _kpis(_uc(db, targets).executive(_filters()))
        assert k["accounting_rate"].light == "red"  # 37.5 < alerta 40
        assert k["accounting_rate"].target == 90
        assert k["error_rate"].light == "red"  # 12.5 > 5
        assert k["documents_total"].light is None  # neutral: nunca semáforo
        assert k["pending_approval"].target is None and k["pending_approval"].light is None

    def test_filtro_por_proveedor(self, db):
        k = _kpis(_uc(db).executive(_filters(issuer_nit="800000001")))
        assert k["documents_total"].value == 1
        assert k["approved_not_accounted"].value == 1

    def test_filtro_por_estado(self, db):
        k = _kpis(_uc(db).executive(_filters(status=300)))
        assert k["documents_total"].value == 2
        assert k["accounting_rate"].value == 0.0  # hay base (2 docs) y ninguno contabilizado


# ── Vista de operación ──────────────────────────────────────────────────────────────────


class TestOperations:
    def test_conteo_por_estado_trae_los_cinco(self, db):
        resp = _uc(db).operations(_filters(), "day")
        by = {s.status: s for s in resp.by_status}
        assert set(by) == {0, 100, 200, 300, 400}
        assert by[400].count == 3 and by[400].amount == 11000.0
        assert by[300].count == 2
        assert by[100].count == 1

    def test_embudo_acumulativo(self, db):
        resp = _uc(db).operations(_filters(), "day")
        f = {s.stage: s for s in resp.funnel}
        # El error de contabilización (doc 6) ya pasó por causación y aprobación.
        assert [f[x].count for x in ("processed", "caused", "approved", "accounted")] == [
            8,
            7,
            6,
            3,
        ]
        assert f["processed"].pct_of_total == 100.0
        assert f["processed"].conversion_from_previous_pct is None
        assert f["caused"].conversion_from_previous_pct == 87.5
        assert f["accounted"].pct_of_total == 37.5

    def test_embudo_vacio(self, db):
        resp = _uc(db).operations(
            _filters(from_date=date(2025, 1, 1), to_date=date(2025, 1, 2)), "day"
        )
        assert all(s.count == 0 and s.pct_of_total is None for s in resp.funnel)

    def test_backlog_por_etapa_y_antiguedad_ignora_el_periodo(self, db):
        """El doc 10 (septiembre, procesado, 32 días) está fuera del periodo pero SÍ es backlog."""
        resp = _uc(db).operations(_filters(), "day")
        b = {s.stage: {r.range: r.count for r in s.ranges} for s in resp.backlog}
        assert b["processed"] == {"0-3": 1, "4-7": 0, "8-15": 0, "15+": 1}
        assert b["caused"] == {"0-3": 1, "4-7": 0, "8-15": 0, "15+": 0}
        assert b["approved"] == {"0-3": 0, "4-7": 1, "8-15": 0, "15+": 1}
        assert b["error"] == {"0-3": 0, "4-7": 0, "8-15": 1, "15+": 0}
        assert [s.count for s in resp.backlog] == [2, 1, 2, 1]

    def test_serie_semanal_y_tendencia_excluye_el_bucket_en_curso(self, db):
        series = _uc(db).operations(_filters(), "week").series
        assert [(p.from_date, p.count) for p in series.points] == [
            (date(2026, 9, 28), 1),
            (date(2026, 10, 5), 2),
            (date(2026, 10, 12), 1),
            (date(2026, 10, 19), 4),
        ]
        assert series.points[1].count_variation_pct == 100.0
        # El bucket del 19 al 25 sigue en curso: la tendencia compara 1 contra 2 → baja.
        assert series.trend == "down"

    def test_serie_mensual_con_un_solo_bucket_no_tiene_tendencia(self, db):
        series = _uc(db).operations(_filters(), "month").series
        assert [(p.from_date, p.to_date, p.count) for p in series.points] == [
            (date(2026, 10, 1), date(2026, 10, 31), 8)
        ]
        assert series.trend is None

    def test_serie_diaria_omite_dias_vacios(self, db):
        series = _uc(db).operations(_filters(), "day").series
        assert len(series.points) == 8 and series.granularity == "day"


# ── Drill-down ──────────────────────────────────────────────────────────────────────────


class TestDrilldown:
    def test_cada_kpi_coincide_con_su_drilldown(self, db):
        """Invariante central: la cifra de la tarjeta y la lista detrás de ella son la misma."""
        uc = _uc(db)
        for f in (_filters(), _filters(issuer_nit="900123456"), _filters(status=300)):
            for kpi in uc.executive(f).kpis:
                if kpi.drilldown is None or kpi.code in ("accounting_rate", "error_rate"):
                    continue
                if kpi.unit == "percentage":
                    continue
                assert uc.drilldown(f, kpi.drilldown).total == kpi.value, (kpi.code, f)

    def test_tasas_se_explican_con_su_numerador(self, db):
        uc = _uc(db)
        assert uc.drilldown(_filters(), "accounted").total == 3
        assert uc.drilldown(_filters(), "in_error").total == 1
        out = uc.drilldown(_filters(), "accounted_outside_period")
        assert [i.document_number for i in out.items] == ["FE2"]

    def test_backlog_coincide_con_la_celda_y_va_del_mas_antiguo(self, db):
        uc = _uc(db)
        resp = uc.operations(_filters(), "day")
        for stage in resp.backlog:
            for rng in stage.ranges:
                got = uc.drilldown(_filters(), "backlog", stage=stage.stage, age_range=rng.range)
                assert got.total == rng.count, (stage.stage, rng.range)
                assert got.amount == rng.amount
        out = uc.drilldown(_filters(), "backlog", stage="approved")
        assert [i.document_number for i in out.items] == ["FE4", "FE3"]  # 17 días, luego 7
        assert [i.age_days for i in out.items] == [17, 7]

    def test_paginacion_conserva_total_e_importe(self, db):
        out = _uc(db).drilldown(_filters(), "documents", limit=3, offset=3)
        assert out.total == 8 and out.amount == 36000.0
        assert len(out.items) == 3 and out.limit == 3 and out.offset == 3

    def test_pagina_fuera_de_rango(self, db):
        out = _uc(db).drilldown(_filters(), "documents", limit=10, offset=100)
        assert out.total == 8 and out.items == []

    def test_item_trae_estado_y_error(self, db):
        out = _uc(db).drilldown(_filters(), "in_error")
        item = out.items[0]
        assert (item.status, item.status_name, item.age_days) == (0, "Error", 12)
        assert item.accounting_error == "cuenta inválida"


# ── Configuración ───────────────────────────────────────────────────────────────────────


class TestTargetsConfig:
    def test_json_valido(self, monkeypatch):
        monkeypatch.setenv(
            "DASHBOARD_TARGETS", '{"error_rate":{"meta":2,"alerta":5},"x":{"meta":"abc"},"y":3}'
        )
        t = load_targets()
        assert t == {"error_rate": Target(meta=2.0, alerta=5.0)}

    @pytest.mark.parametrize("raw", ["", "no es json", "[1,2]", "{}"])
    def test_invalido_o_vacio_deja_sin_metas_y_no_revienta(self, monkeypatch, raw):
        monkeypatch.setenv("DASHBOARD_TARGETS", raw)
        assert load_targets() == {}


# ── HTTP ────────────────────────────────────────────────────────────────────────────────


@pytest.fixture
def client(db):
    app.dependency_overrides[get_dashboard_use_case] = lambda: _uc(
        db, {"error_rate": Target(meta=2, alerta=5)}
    )
    yield TestClient(app)
    app.dependency_overrides.clear()


Q = "from_date=2026-10-01&to_date=2026-10-22"


class TestHttp:
    def test_requiere_autenticacion(self):
        c = TestClient(app)
        for path in ("executive", "operations", "documents?indicator=documents"):
            assert c.get(f"/api/v1/dashboard/{path}").status_code in (401, 403), path

    def test_executive(self, client):
        r = client.get(f"/api/v1/dashboard/executive?{Q}")
        assert r.status_code == 200
        body = r.json()
        assert body["period"]["previous_from_date"] == "2026-09-09"
        by = {k["code"]: k for k in body["kpis"]}
        assert by["error_rate"]["value"] == 12.5 and by["error_rate"]["light"] == "red"
        assert by["approved_not_accounted"]["drilldown"] == "approved_not_accounted"
        assert set(by["error_rate"]) >= {
            "code",
            "name",
            "unit",
            "direction",
            "value",
            "previous_value",
            "variation_pct",
            "target",
            "warning",
            "light",
            "drilldown",
            "note",
        }

    def test_operations_con_granularidad(self, client):
        r = client.get(f"/api/v1/dashboard/operations?{Q}&granularity=week")
        assert r.status_code == 200
        body = r.json()
        assert body["series"]["granularity"] == "week" and body["series"]["trend"] == "down"
        assert [s["stage"] for s in body["backlog"]] == ["processed", "caused", "approved", "error"]
        assert [s["stage"] for s in body["funnel"]] == [
            "processed",
            "caused",
            "approved",
            "accounted",
        ]

    def test_drilldown_backlog(self, client):
        r = client.get(
            f"/api/v1/dashboard/documents?{Q}&indicator=backlog&stage=approved&age_range=15%2B"
        )
        assert r.status_code == 200
        body = r.json()
        assert body["total"] == 1 and body["items"][0]["document_number"] == "FE4"

    @pytest.mark.parametrize(
        "query",
        [
            "executive?from_date=2026-10-22&to_date=2026-10-01",  # rango invertido
            "executive?from_date=2020-01-01&to_date=2026-10-01",  # más de 731 días
            "executive?status=999",
            "operations?granularity=year",
            "documents?indicator=nada",
            "documents?indicator=backlog",  # falta stage
            "documents?indicator=backlog&stage=approved&age_range=99",
            "documents?indicator=accounted&stage=approved",  # stage con otro indicador
            "documents?indicator=documents&limit=0",
            "documents?indicator=documents&limit=501",
            "documents?indicator=documents&offset=-1",
        ],
    )
    def test_entradas_invalidas_son_422(self, client, query):
        assert client.get(f"/api/v1/dashboard/{query}").status_code == 422

    def test_sin_fechas_usa_ventana_de_30_dias_hasta_hoy(self, client, monkeypatch):
        monkeypatch.setattr("app.adapters.api.routers.dashboard.today_local", lambda: TODAY)
        body = client.get("/api/v1/dashboard/executive").json()
        assert body["period"]["from_date"] == "2026-09-23"
        assert body["period"]["to_date"] == "2026-10-22"

    def test_openapi_documenta_los_tres_endpoints(self):
        paths = TestClient(app).get("/openapi.json").json()["paths"]
        for p in (
            "/api/v1/dashboard/executive",
            "/api/v1/dashboard/operations",
            "/api/v1/dashboard/documents",
        ):
            op = paths[p]["get"]
            assert op["summary"] and op["description"] and "422" in op["responses"], p
