"""Casos de uso del dashboard: vista ejecutiva, vista de operación y drill-down.

FÓRMULAS (el diccionario completo está en `docs/dashboard-indicadores.md`):

Universo. Todo indicador del periodo cuenta los documentos cuya **fecha de emisión**
(`documents.date`) cae en `[from_date, to_date]` y los clasifica por su **estado actual**.
No existe historial de transiciones, así que NO se puede afirmar «en el periodo se
contabilizaron N»; se afirma «de los documentos emitidos en el periodo, N están hoy
contabilizados». El periodo anterior comparable se calcula con exactamente la misma regla.

- `accounting_rate`        = contabilizados / total × 100
- `error_rate`             = en ERROR / total × 100
- `accounted_on_time_rate` = contabilizados cuyo mes de `accounted_at` (zona del dashboard)
                             es el mes de `date` / contabilizados con `accounted_at` × 100
- `without_support`        = documentos sin `pdf_url`
- `duplicates`             = filas de `processing_logs` con status `duplicate` en el periodo
                             (sin filtros de documento: el log no guarda proveedor ni empresa)

Embudo (acumulativo, por estado actual): procesados = todos; causados = CAUSADO + APROBADO +
CONTABILIZADA + ERROR de contabilización; aprobados = APROBADO + CONTABILIZADA + ERROR de
contabilización; contabilizados = CONTABILIZADA. Un ERROR de contabilización ya pasó por
causación y aprobación; un ERROR sin `accounting_error` (XML no procesable) no pasa de
«procesados». «Descargados» no existe: no se guarda cuándo ni cómo se descargó.
"""

from datetime import date, datetime, timezone
from typing import Callable, Optional

from app.application.dto.dashboard import (
    AgeRange,
    AppliedFilters,
    BacklogStage,
    DashboardFilters,
    DrilldownDocument,
    DrilldownResponse,
    ExecutiveDashboardResponse,
    FunnelStage,
    KpiResponse,
    OperationsDashboardResponse,
    PeriodResponse,
    SeriesPoint,
    StatusCount,
    TimeSeries,
)
from app.domain.services.dashboard_metrics import (
    AGE_BUCKETS,
    HIGHER_IS_BETTER,
    LOWER_IS_BETTER,
    NEUTRAL,
    bucket_end,
    previous_period,
    safe_ratio_pct,
    traffic_light,
    trend,
    variation_pct,
)
from app.domain.value_objects.document_status import DocumentStatus
from app.infrastructure.config.dashboard_settings import Target
from app.infrastructure.persistence.repositories.dashboard_repository import (
    DashboardRepository,
)

#: Etapa del backlog → estado del documento que la define, y su rótulo.
BACKLOG_STAGES: tuple[tuple[str, int, str], ...] = (
    ("processed", DocumentStatus.PROCESADO, "Procesados sin causar"),
    ("caused", DocumentStatus.CAUSADO, "Causados sin aprobar"),
    ("approved", DocumentStatus.APROBADO, "Aprobados sin contabilizar"),
    ("error", DocumentStatus.ERROR, "En error"),
)

#: Indicadores de drill-down válidos para `GET /dashboard/documents`.
DRILLDOWN_INDICATORS = (
    "documents",
    "accounted",
    "pending_approval",
    "approved_not_accounted",
    "in_error",
    "without_support",
    "accounted_outside_period",
    "backlog",
)


def _applied(f: DashboardFilters) -> AppliedFilters:
    return AppliedFilters(
        receiver_nit=f.receiver_nit,
        issuer_nit=f.issuer_nit,
        cost_center_id=f.cost_center_id,
        status=f.status,
    )


def _has_document_filters(f: DashboardFilters) -> bool:
    return any(
        v is not None and v != ""
        for v in (f.receiver_nit, f.issuer_nit, f.cost_center_id, f.status)
    )


class DashboardUseCase:
    def __init__(
        self,
        repo: DashboardRepository,
        targets: Optional[dict[str, Target]] = None,
        today: Optional[Callable[[], date]] = None,
        now: Optional[Callable[[], datetime]] = None,
    ):
        self.repo = repo
        self.targets = targets or {}
        # Reloj inyectable: las fórmulas de antigüedad y tendencia dependen de «hoy».
        self._today = today or date.today
        self._now = now or (lambda: datetime.now(timezone.utc))

    # ── Ejecutivo ───────────────────────────────────────────────────────────────────────

    def executive(self, f: DashboardFilters) -> ExecutiveDashboardResponse:
        prev_start, prev_end = previous_period(f.from_date, f.to_date)
        cur = self.repo.kpi_aggregates(f, f.from_date, f.to_date)
        prev = self.repo.kpi_aggregates(f, prev_start, prev_end)

        dup_note = None
        if _has_document_filters(f):
            cur_dup = prev_dup = None
            dup_note = (
                "Los duplicados se registran por archivo, sin proveedor ni empresa: "
                "no se puede aplicar ningún filtro de documento a este indicador."
            )
        else:
            cur_dup = self.repo.duplicates_count(f.from_date, f.to_date)
            prev_dup = self.repo.duplicates_count(prev_start, prev_end)

        def rates(a: dict) -> dict:
            return {
                "accounting_rate": safe_ratio_pct(a["accounted"], a["total"]),
                "error_rate": safe_ratio_pct(a["in_error"], a["total"]),
                "on_time": safe_ratio_pct(a["accounted_on_time"], a["accounted_dated"]),
            }

        cr, pr = rates(cur), rates(prev)
        kpis = [
            self._kpi(
                "documents_total",
                "Documentos del periodo",
                "count",
                NEUTRAL,
                cur["total"],
                prev["total"],
                "documents",
            ),
            self._kpi(
                "amount_total",
                "Valor total",
                "currency",
                NEUTRAL,
                cur["amount"],
                prev["amount"],
                None,
            ),
            self._kpi(
                "accounting_rate",
                "Tasa de contabilización",
                "percentage",
                HIGHER_IS_BETTER,
                cr["accounting_rate"],
                pr["accounting_rate"],
                "accounted",
            ),
            self._kpi(
                "error_rate",
                "Tasa de error",
                "percentage",
                LOWER_IS_BETTER,
                cr["error_rate"],
                pr["error_rate"],
                "in_error",
            ),
            self._kpi(
                "pending_approval",
                "Pendientes de aprobación",
                "count",
                LOWER_IS_BETTER,
                cur["pending_approval"],
                prev["pending_approval"],
                "pending_approval",
            ),
            self._kpi(
                "approved_not_accounted",
                "Aprobados sin contabilizar",
                "count",
                LOWER_IS_BETTER,
                cur["approved_not_accounted"],
                prev["approved_not_accounted"],
                "approved_not_accounted",
            ),
            self._kpi(
                "accounted_on_time_rate",
                "Contabilizados en el periodo correcto",
                "percentage",
                HIGHER_IS_BETTER,
                cr["on_time"],
                pr["on_time"],
                "accounted_outside_period",
                note="El drill-down lista los contabilizados FUERA del periodo correcto.",
            ),
            self._kpi(
                "without_support",
                "Documentos sin soporte (PDF)",
                "count",
                LOWER_IS_BETTER,
                cur["without_support"],
                prev["without_support"],
                "without_support",
            ),
            self._kpi(
                "duplicates",
                "Duplicados detectados",
                "count",
                LOWER_IS_BETTER,
                cur_dup,
                prev_dup,
                None,
                note=dup_note,
            ),
        ]
        return ExecutiveDashboardResponse(
            period=PeriodResponse(
                from_date=f.from_date,
                to_date=f.to_date,
                previous_from_date=prev_start,
                previous_to_date=prev_end,
            ),
            filters=_applied(f),
            kpis=kpis,
            generated_at=self._now(),
        )

    def _kpi(
        self, code, name, unit, direction, value, previous, drilldown, note=None
    ) -> KpiResponse:
        target = self.targets.get(code)
        meta = target.meta if target else None
        warning = target.alerta if target else None
        return KpiResponse(
            code=code,
            name=name,
            unit=unit,
            direction=direction,
            value=value,
            previous_value=previous,
            variation_pct=variation_pct(value, previous),
            target=meta,
            warning=warning,
            light=traffic_light(value, direction, meta, warning),
            drilldown=drilldown,
            note=note,
        )

    # ── Operación ───────────────────────────────────────────────────────────────────────

    def operations(self, f: DashboardFilters, granularity: str) -> OperationsDashboardResponse:
        prev_start, prev_end = previous_period(f.from_date, f.to_date)
        today = self._today()
        by_status = self.repo.status_breakdown(f, f.from_date, f.to_date)
        err_acc = self.repo.errors_after_approval(f, f.from_date, f.to_date)

        return OperationsDashboardResponse(
            period=PeriodResponse(
                from_date=f.from_date,
                to_date=f.to_date,
                previous_from_date=prev_start,
                previous_to_date=prev_end,
            ),
            filters=_applied(f),
            by_status=[
                StatusCount(
                    status=code,
                    name=DocumentStatus.NAMES[code],
                    count=by_status.get(code, {}).get("count", 0),
                    amount=by_status.get(code, {}).get("amount", 0.0),
                )
                for code in sorted(DocumentStatus.NAMES)
            ],
            funnel=self._funnel(by_status, err_acc),
            series=self._series(f, granularity, today),
            backlog=self._backlog(f, today),
            generated_at=self._now(),
        )

    @staticmethod
    def _funnel(by_status: dict, err_acc: dict) -> list[FunnelStage]:
        def total(key: str, *codes: int) -> float:
            return sum(by_status.get(c, {}).get(key, 0) for c in codes)

        S = DocumentStatus
        stages = [
            (
                "processed",
                "Procesados",
                total("count", *DocumentStatus.NAMES),
                total("amount", *DocumentStatus.NAMES),
            ),
            (
                "caused",
                "Causados",
                total("count", S.CAUSADO, S.APROBADO, S.CONTABILIZADA) + err_acc["count"],
                total("amount", S.CAUSADO, S.APROBADO, S.CONTABILIZADA) + err_acc["amount"],
            ),
            (
                "approved",
                "Aprobados",
                total("count", S.APROBADO, S.CONTABILIZADA) + err_acc["count"],
                total("amount", S.APROBADO, S.CONTABILIZADA) + err_acc["amount"],
            ),
            (
                "accounted",
                "Contabilizados",
                total("count", S.CONTABILIZADA),
                total("amount", S.CONTABILIZADA),
            ),
        ]
        first = stages[0][2]
        out, previous = [], None
        for code, name, count, amount in stages:
            out.append(
                FunnelStage(
                    stage=code,
                    name=name,
                    count=int(count),
                    amount=round(amount, 2),
                    pct_of_total=safe_ratio_pct(count, first),
                    conversion_from_previous_pct=(
                        None if previous is None else safe_ratio_pct(count, previous)
                    ),
                )
            )
            previous = count
        return out

    def _series(self, f: DashboardFilters, granularity: str, today: date) -> TimeSeries:
        rows = self.repo.time_series(f, f.from_date, f.to_date, granularity)
        points, prev_count = [], None
        for row in rows:
            points.append(
                SeriesPoint(
                    from_date=row["start"],
                    to_date=bucket_end(row["start"], granularity),
                    count=row["count"],
                    amount=row["amount"],
                    count_variation_pct=variation_pct(row["count"], prev_count),
                )
            )
            prev_count = row["count"]
        # Solo buckets terminados: el que sigue en curso parecería una caída falsa.
        complete = [p.count for p in points if p.to_date < today]
        return TimeSeries(granularity=granularity, points=points, trend=trend(complete))

    def _backlog(self, f: DashboardFilters, today: date) -> list[BacklogStage]:
        data = self.repo.backlog(f, today)
        out = []
        for stage, status, name in BACKLOG_STAGES:
            ranges = [
                AgeRange(
                    range=label,
                    count=data.get((status, label), {}).get("count", 0),
                    amount=data.get((status, label), {}).get("amount", 0.0),
                )
                for label, _, _ in AGE_BUCKETS
            ]
            out.append(
                BacklogStage(
                    stage=stage,
                    status=status,
                    name=name,
                    count=sum(r.count for r in ranges),
                    amount=round(sum(r.amount for r in ranges), 2),
                    ranges=ranges,
                )
            )
        return out

    # ── Drill-down ──────────────────────────────────────────────────────────────────────

    def drilldown(
        self,
        f: DashboardFilters,
        indicator: str,
        *,
        stage: Optional[str] = None,
        age_range: Optional[str] = None,
        limit: int = 50,
        offset: int = 0,
    ) -> DrilldownResponse:
        """Documentos detrás de un indicador, con las MISMAS reglas que su agregado.

        Para cada indicador, `total` coincide con el valor del KPI (o con la celda del
        backlog) calculado con los mismos filtros: se prueba explícitamente.
        """
        S = DocumentStatus
        today = self._today()
        total, amount, rows = self.repo.drilldown(
            f,
            indicator,
            stage=stage,
            age_range=age_range,
            today=today,
            limit=limit,
            offset=offset,
        )
        items = [
            DrilldownDocument(
                id=r.id,
                document_number=r.document_number,
                issuer_name=r.issuer_name,
                issuer_nit=r.issuer_nit,
                receiver_nit=r.receiver_nit,
                date=r.date,
                age_days=max((today - r.date).days, 0),
                total=r.total,
                status=r.status,
                status_name=S.NAMES.get(r.status, str(r.status)),
                cost_center_id=r.cost_center_id,
                accounting_error=r.accounting_error,
            )
            for r in rows
        ]
        return DrilldownResponse(
            indicator=indicator,
            total=total,
            amount=round(amount, 2),
            limit=limit,
            offset=offset,
            items=items,
        )
