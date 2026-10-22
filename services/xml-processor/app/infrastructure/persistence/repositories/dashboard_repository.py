"""Consultas de agregación del dashboard.

Regla de este módulo: **todo se calcula en la base**. Ninguna función trae documentos a
memoria para sumarlos o contarlos; cada una devuelve ya el agregado (una fila por estado,
por bucket o por rango). El drill-down es la única que devuelve filas de documento, y siempre
paginadas.

Las consultas son portables entre PostgreSQL (producción) y SQLite (pruebas): las dos únicas
expresiones que difieren —truncar una fecha a semana/mes y formatear año-mes— están aisladas
en `_bucket_expr` y `_year_month`.
"""

from datetime import date, datetime, time, timedelta
from typing import Optional

from sqlalchemy import Date, Numeric, and_, case, cast, func
from sqlalchemy.orm import Session

from app.application.dto.dashboard import DashboardFilters
from app.domain.services.dashboard_metrics import AGE_BUCKETS, age_bucket_date_range
from app.domain.value_objects.document_status import DocumentStatus
from app.infrastructure.persistence.models.document import Document
from app.infrastructure.persistence.models.processing_log import ProcessingLog

#: Etapa del backlog → estado del documento que la define. Es la fuente única: el caso de uso
#: la importa para rotular las etapas.
BACKLOG_STATUS_BY_STAGE = {
    "processed": DocumentStatus.PROCESADO,
    "caused": DocumentStatus.CAUSADO,
    "approved": DocumentStatus.APROBADO,
    "error": DocumentStatus.ERROR,
}


def _amount_sum():
    # `total` es Float: se castea a NUMERIC para que la suma no arrastre error binario.
    return func.coalesce(func.sum(cast(Document.total, Numeric(18, 2))), 0)


def _count_if(condition):
    return func.coalesce(func.sum(case((condition, 1), else_=0)), 0)


class DashboardRepository:
    def __init__(self, db: Session, timezone: str = "America/Bogota"):
        self.db = db
        self.timezone = timezone

    # ── Utilidades de dialecto ──────────────────────────────────────────────────────────

    @property
    def _is_postgres(self) -> bool:
        return self.db.get_bind().dialect.name == "postgresql"

    def _year_month(self, column, *, localize: bool = False):
        """`'YYYY-MM'` de una fecha o instante. `localize` convierte antes a la zona configurada."""
        if self._is_postgres:
            expr = func.timezone(self.timezone, column) if localize else column
            return func.to_char(expr, "YYYY-MM")
        return func.strftime("%Y-%m", column)

    def _bucket_expr(self, granularity: str):
        """Primer día del bucket (`day`, `week` = lunes, `month`) de `documents.date`."""
        if self._is_postgres:
            unit = {"day": "day", "week": "week", "month": "month"}[granularity]
            return cast(func.date_trunc(unit, Document.date), Date)
        if granularity == "day":
            return Document.date
        if granularity == "week":
            return func.date(Document.date, "weekday 0", "-6 days")
        return func.date(Document.date, "start of month")

    # ── Filtros ─────────────────────────────────────────────────────────────────────────

    @staticmethod
    def _filters(f: DashboardFilters, start: Optional[date], end: Optional[date]) -> list:
        """Condiciones comunes. `start`/`end` en None omiten el rango de fechas (backlog)."""
        conds = []
        if start is not None:
            conds.append(Document.date >= start)
        if end is not None:
            conds.append(Document.date <= end)
        if f.receiver_nit:
            conds.append(Document.receiver_nit == f.receiver_nit)
        if f.issuer_nit:
            conds.append(Document.issuer_nit == f.issuer_nit)
        if f.cost_center_id is not None:
            conds.append(Document.cost_center_id == f.cost_center_id)
        if f.status is not None:
            conds.append(Document.status == f.status)
        return conds

    # ── Indicadores ejecutivos ──────────────────────────────────────────────────────────

    def kpi_aggregates(self, f: DashboardFilters, start: date, end: date) -> dict:
        """Todos los contadores de la vista ejecutiva en UNA sola pasada sobre `documents`."""
        accounted = Document.status == DocumentStatus.CONTABILIZADA
        accounted_dated = and_(accounted, Document.accounted_at.isnot(None))
        on_time = and_(
            accounted_dated,
            self._year_month(Document.accounted_at, localize=True)
            == self._year_month(Document.date),
        )
        row = (
            self.db.query(
                func.count(Document.id),
                _amount_sum(),
                _count_if(accounted),
                _count_if(Document.status == DocumentStatus.CAUSADO),
                _count_if(Document.status == DocumentStatus.APROBADO),
                _count_if(Document.status == DocumentStatus.ERROR),
                _count_if(Document.pdf_url.is_(None)),
                _count_if(accounted_dated),
                _count_if(on_time),
            )
            .filter(*self._filters(f, start, end))
            .one()
        )
        return {
            "total": int(row[0] or 0),
            "amount": float(row[1] or 0),
            "accounted": int(row[2] or 0),
            "pending_approval": int(row[3] or 0),
            "approved_not_accounted": int(row[4] or 0),
            "in_error": int(row[5] or 0),
            "without_support": int(row[6] or 0),
            "accounted_dated": int(row[7] or 0),
            "accounted_on_time": int(row[8] or 0),
        }

    def duplicates_count(self, start: date, end: date) -> int:
        """Archivos rechazados por duplicados en el periodo (`processing_logs`).

        El log no guarda proveedor, centro de costo ni empresa, así que los filtros de
        documento no pueden aplicarse a este conteo.
        """
        since = datetime.combine(start, time.min)
        until = datetime.combine(end + timedelta(days=1), time.min)
        return int(
            self.db.query(func.count(ProcessingLog.id))
            .filter(
                ProcessingLog.status == "duplicate",
                ProcessingLog.processed_at >= since,
                ProcessingLog.processed_at < until,
            )
            .scalar()
            or 0
        )

    # ── Operación ───────────────────────────────────────────────────────────────────────

    def status_breakdown(self, f: DashboardFilters, start: date, end: date) -> dict[int, dict]:
        rows = (
            self.db.query(Document.status, func.count(Document.id), _amount_sum())
            .filter(*self._filters(f, start, end))
            .group_by(Document.status)
            .all()
        )
        return {int(s): {"count": int(c), "amount": float(a or 0)} for s, c, a in rows}

    def errors_after_approval(self, f: DashboardFilters, start: date, end: date) -> dict:
        """ERROR de contabilización: el documento ya pasó por causación y aprobación.

        Un ERROR sin `accounting_error` nació así (el XML no se pudo procesar) y nunca estuvo
        en una etapa posterior; no se cuenta en el embudo más allá de «procesados».
        """
        row = (
            self.db.query(func.count(Document.id), _amount_sum())
            .filter(
                *self._filters(f, start, end),
                Document.status == DocumentStatus.ERROR,
                Document.accounting_error.isnot(None),
            )
            .one()
        )
        return {"count": int(row[0] or 0), "amount": float(row[1] or 0)}

    def time_series(
        self, f: DashboardFilters, start: date, end: date, granularity: str
    ) -> list[dict]:
        bucket = self._bucket_expr(granularity)
        rows = (
            self.db.query(bucket, func.count(Document.id), _amount_sum())
            .filter(*self._filters(f, start, end))
            .group_by(bucket)
            .order_by(bucket)
            .all()
        )
        out = []
        for b, c, a in rows:
            day = date.fromisoformat(b) if isinstance(b, str) else b
            out.append({"start": day, "count": int(c), "amount": float(a or 0)})
        return out

    def backlog(self, f: DashboardFilters, today: date) -> dict[tuple[int, str], dict]:
        """Pendientes por estado y rango de antigüedad. NO aplica el rango de fechas."""
        whens = []
        for label, _, _ in AGE_BUCKETS[:-1]:
            oldest, newest = age_bucket_date_range(label, today)
            parts = []
            if oldest is not None:
                parts.append(Document.date >= oldest)
            if newest is not None:
                parts.append(Document.date <= newest)
            whens.append((and_(*parts), label))
        age_range = case(*whens, else_=AGE_BUCKETS[-1][0])

        unfinished = (
            DocumentStatus.PROCESADO,
            DocumentStatus.CAUSADO,
            DocumentStatus.APROBADO,
            DocumentStatus.ERROR,
        )
        rows = (
            self.db.query(Document.status, age_range, func.count(Document.id), _amount_sum())
            .filter(*self._filters(f, None, None), Document.status.in_(unfinished))
            .group_by(Document.status, age_range)
            .all()
        )
        return {(int(s), r): {"count": int(c), "amount": float(a or 0)} for s, r, c, a in rows}

    # ── Drill-down ──────────────────────────────────────────────────────────────────────

    def _drilldown_conditions(
        self, indicator: str, stage: Optional[str], age_range: Optional[str], today: date
    ) -> tuple[list, bool, bool]:
        """`(condiciones, usa_periodo, más_antiguo_primero)` de un indicador.

        Cada rama replica EXACTAMENTE la condición con que `kpi_aggregates` / `backlog`
        cuentan ese indicador; si divergieran, el KPI diría 37 y la lista mostraría otra cifra.
        """
        S = DocumentStatus
        if indicator == "documents":
            return [], True, False
        if indicator == "accounted":
            return [Document.status == S.CONTABILIZADA], True, False
        if indicator == "pending_approval":
            return [Document.status == S.CAUSADO], True, False
        if indicator == "approved_not_accounted":
            return [Document.status == S.APROBADO], True, False
        if indicator == "in_error":
            return [Document.status == S.ERROR], True, False
        if indicator == "without_support":
            return [Document.pdf_url.is_(None)], True, False
        if indicator == "accounted_outside_period":
            cond = and_(
                Document.status == S.CONTABILIZADA,
                Document.accounted_at.isnot(None),
                self._year_month(Document.accounted_at, localize=True)
                != self._year_month(Document.date),
            )
            return [cond], True, False
        if indicator == "backlog":
            status = BACKLOG_STATUS_BY_STAGE[stage]
            conds = [Document.status == status]
            if age_range:
                oldest, newest = age_bucket_date_range(age_range, today)
                if oldest is not None:
                    conds.append(Document.date >= oldest)
                if newest is not None:
                    conds.append(Document.date <= newest)
            # El backlog es lo pendiente HOY: ignora la ventana de emisión y va del más viejo.
            return conds, False, True
        raise ValueError(f"Indicador desconocido: {indicator}")

    def drilldown(
        self,
        f: DashboardFilters,
        indicator: str,
        *,
        stage: Optional[str],
        age_range: Optional[str],
        today: date,
        limit: int,
        offset: int,
    ) -> tuple[int, float, list]:
        """Página de documentos detrás de un indicador, con los filtros comunes.

        Devuelve `(total, importe_total, filas)`. Consulta columnas sueltas y no la entidad:
        `Document` arrastra `pdf_data`/`xml_data` (BYTEA) que un listado nunca necesita.
        """
        conditions, use_period, oldest_first = self._drilldown_conditions(
            indicator, stage, age_range, today
        )
        where = [
            *self._filters(
                f, f.from_date if use_period else None, f.to_date if use_period else None
            ),
            *conditions,
        ]
        total, amount = self.db.query(func.count(Document.id), _amount_sum()).filter(*where).one()
        order = (
            (Document.date.asc(), Document.id.asc())
            if oldest_first
            else (Document.date.desc(), Document.id.desc())
        )
        rows = (
            self.db.query(
                Document.id,
                Document.document_number,
                Document.issuer_name,
                Document.issuer_nit,
                Document.receiver_nit,
                Document.date,
                Document.total,
                Document.status,
                Document.cost_center_id,
                Document.accounting_error,
            )
            .filter(*where)
            .order_by(*order)
            .limit(limit)
            .offset(offset)
            .all()
        )
        return int(total or 0), float(amount or 0), rows
