"""Contratos del dashboard de indicadores.

Todos los indicadores comparten la misma forma (`KpiResponse`) para que el frontend pinte una
tarjeta genérica sin conocer cada indicador. Los nulos son significativos: `None` en un valor
o una variación quiere decir «no hay base para calcularlo», no «cero».
"""

from dataclasses import dataclass
from datetime import date, datetime
from datetime import date as Date  # alias: el campo `date` de un modelo pisa el tipo `date`
from typing import Literal, Optional

from pydantic import BaseModel, Field

Unit = Literal["count", "currency", "percentage"]
Direction = Literal["higher_is_better", "lower_is_better", "neutral"]
Light = Literal["green", "yellow", "red"]
Granularity = Literal["day", "week", "month"]


@dataclass(frozen=True)
class DashboardFilters:
    """Filtros comunes a todas las vistas. Se aplican siempre con AND."""

    from_date: date
    to_date: date
    receiver_nit: Optional[str] = None
    issuer_nit: Optional[str] = None
    cost_center_id: Optional[int] = None
    status: Optional[int] = None


class AppliedFilters(BaseModel):
    receiver_nit: Optional[str] = Field(
        None, description="NIT de la empresa receptora (compradora).", examples=["900111222"]
    )
    issuer_nit: Optional[str] = Field(
        None, description="NIT del proveedor emisor.", examples=["900123456"]
    )
    cost_center_id: Optional[int] = Field(None, description="Centro de costo.", examples=[4])
    status: Optional[int] = Field(
        None,
        description="Estado del documento: 0 Error, 100 Procesado, 200 Causado, 300 Aprobado, 400 Contabilizada.",
        examples=[300],
    )


class PeriodResponse(BaseModel):
    from_date: date = Field(description="Inicio del periodo (inclusive).", examples=["2026-10-01"])
    to_date: date = Field(description="Fin del periodo (inclusive).", examples=["2026-10-22"])
    previous_from_date: date = Field(
        description="Inicio del periodo anterior comparable (misma duración).",
        examples=["2026-09-09"],
    )
    previous_to_date: date = Field(
        description="Fin del periodo anterior comparable.", examples=["2026-09-30"]
    )


class KpiResponse(BaseModel):
    code: str = Field(description="Código estable del indicador.", examples=["accounting_rate"])
    name: str = Field(description="Nombre para mostrar.", examples=["Tasa de contabilización"])
    unit: Unit = Field(description="Cómo interpretar `value`.", examples=["percentage"])
    direction: Direction = Field(
        description="Si es mejor un valor alto, uno bajo o ninguno (no lleva semáforo).",
        examples=["higher_is_better"],
    )
    value: Optional[float] = Field(
        None,
        description="Valor en el periodo. `null` si no hay base para calcularlo.",
        examples=[82.5],
    )
    previous_value: Optional[float] = Field(
        None, description="Mismo cálculo en el periodo anterior comparable.", examples=[75.0]
    )
    variation_pct: Optional[float] = Field(
        None,
        description="(valor − anterior) / |anterior| × 100. `null` si el anterior es 0 o no existe.",
        examples=[10.0],
    )
    target: Optional[float] = Field(
        None,
        description="Meta configurada (`DASHBOARD_TARGETS`). `null` si no hay.",
        examples=[90.0],
    )
    warning: Optional[float] = Field(
        None, description="Umbral de la banda amarilla, si está configurado.", examples=[75.0]
    )
    light: Optional[Light] = Field(
        None,
        description="Semáforo frente a la meta. `null` si no hay meta, valor o es un indicador neutral.",
        examples=["yellow"],
    )
    drilldown: Optional[str] = Field(
        None,
        description="Valor de `indicator` para `GET /dashboard/documents` que lista los documentos "
        "detrás de este indicador. `null` si no tiene drill-down.",
        examples=["approved_not_accounted"],
    )
    note: Optional[str] = Field(
        None, description="Salvedad de cálculo (p. ej. un filtro que no aplica a este indicador)."
    )


class ExecutiveDashboardResponse(BaseModel):
    period: PeriodResponse
    filters: AppliedFilters
    kpis: list[KpiResponse]
    generated_at: datetime = Field(description="Instante de cálculo (UTC).")

    model_config = {
        "json_schema_extra": {
            "example": {
                "period": {
                    "from_date": "2026-10-01",
                    "to_date": "2026-10-22",
                    "previous_from_date": "2026-09-09",
                    "previous_to_date": "2026-09-30",
                },
                "filters": {
                    "receiver_nit": None,
                    "issuer_nit": None,
                    "cost_center_id": None,
                    "status": None,
                },
                "kpis": [
                    {
                        "code": "accounting_rate",
                        "name": "Tasa de contabilización",
                        "unit": "percentage",
                        "direction": "higher_is_better",
                        "value": 82.5,
                        "previous_value": 75.0,
                        "variation_pct": 10.0,
                        "target": 90.0,
                        "warning": 75.0,
                        "light": "yellow",
                        "drilldown": "accounted",
                        "note": None,
                    }
                ],
                "generated_at": "2026-10-22T15:04:05Z",
            }
        }
    }


class StatusCount(BaseModel):
    status: int = Field(description="Código de estado.", examples=[300])
    name: str = Field(description="Nombre del estado.", examples=["Aprobado"])
    count: int = Field(description="Documentos en ese estado.", examples=[37])
    amount: float = Field(description="Suma de `total` de esos documentos.", examples=[15234000.0])


class FunnelStage(BaseModel):
    stage: str = Field(
        description="`processed`, `caused`, `approved` o `accounted` (acumulativo).",
        examples=["caused"],
    )
    name: str = Field(examples=["Causados"])
    count: int = Field(
        description="Documentos que llegaron a esta etapa o la superaron.", examples=[120]
    )
    amount: float = Field(examples=[98000000.0])
    pct_of_total: Optional[float] = Field(
        None,
        description="% sobre los documentos procesados. `null` si no hay documentos.",
        examples=[96.0],
    )
    conversion_from_previous_pct: Optional[float] = Field(
        None,
        description="% que pasó desde la etapa anterior. `null` en la primera etapa o sin base.",
        examples=[96.0],
    )


class SeriesPoint(BaseModel):
    from_date: date = Field(description="Primer día del bucket.", examples=["2026-10-05"])
    to_date: date = Field(description="Último día del bucket.", examples=["2026-10-11"])
    count: int = Field(examples=[42])
    amount: float = Field(examples=[12500000.0])
    count_variation_pct: Optional[float] = Field(
        None,
        description="Variación de `count` contra el bucket anterior de la serie.",
        examples=[5.0],
    )


class TimeSeries(BaseModel):
    granularity: Granularity
    points: list[SeriesPoint] = Field(
        description="Buckets con al menos un documento, en orden cronológico. Los vacíos se omiten."
    )
    trend: Optional[Literal["up", "down", "flat"]] = Field(
        None,
        description="Compara los dos últimos buckets COMPLETOS (el bucket en curso se excluye). "
        "`flat` si varía ≤ 1 %. `null` con menos de dos buckets completos o base 0.",
    )


class AgeRange(BaseModel):
    range: str = Field(description="`0-3`, `4-7`, `8-15` o `15+` días.", examples=["15+"])
    count: int = Field(examples=[37])
    amount: float = Field(examples=[8100000.0])


class BacklogStage(BaseModel):
    stage: str = Field(
        description="`processed`, `caused`, `approved` o `error`.", examples=["approved"]
    )
    status: int = Field(description="Estado del documento que define la etapa.", examples=[300])
    name: str = Field(examples=["Aprobados sin contabilizar"])
    count: int = Field(examples=[52])
    amount: float = Field(examples=[31000000.0])
    ranges: list[AgeRange] = Field(description="Siempre los cuatro rangos, aunque estén en 0.")


class OperationsDashboardResponse(BaseModel):
    period: PeriodResponse
    filters: AppliedFilters
    by_status: list[StatusCount]
    funnel: list[FunnelStage]
    series: TimeSeries
    backlog: list[BacklogStage] = Field(
        description="Foto actual de lo no terminado. NO usa el rango de fechas del periodo "
        "(solo los demás filtros): el backlog es lo que está pendiente hoy, sin importar cuándo se emitió."
    )
    generated_at: datetime


class DrilldownDocument(BaseModel):
    id: int = Field(examples=[1024])
    document_number: str = Field(examples=["FE-2231"])
    issuer_name: str = Field(examples=["Proveedor SAS"])
    issuer_nit: str = Field(examples=["900123456"])
    receiver_nit: str = Field(examples=["900111222"])
    date: Date = Field(description="Fecha de emisión.", examples=["2026-09-30"])
    age_days: int = Field(description="Días desde la fecha de emisión hasta hoy.", examples=[22])
    total: float = Field(examples=[1500000.0])
    status: int = Field(examples=[300])
    status_name: str = Field(examples=["Aprobado"])
    cost_center_id: Optional[int] = Field(None, examples=[4])
    accounting_error: Optional[str] = Field(None, description="Motivo si está en Error.")


class DrilldownResponse(BaseModel):
    indicator: str = Field(examples=["approved_not_accounted"])
    total: int = Field(description="Total de documentos que cumplen (para paginar).", examples=[37])
    amount: float = Field(
        description="Suma de `total` de todos los documentos que cumplen.", examples=[8100000.0]
    )
    limit: int = Field(examples=[50])
    offset: int = Field(examples=[0])
    items: list[DrilldownDocument]
