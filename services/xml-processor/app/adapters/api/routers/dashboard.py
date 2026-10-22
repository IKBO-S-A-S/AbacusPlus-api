from datetime import date, timedelta
from typing import Literal, Optional

from fastapi import APIRouter, Depends, HTTPException, Query, status

from app.application.dto.dashboard import (
    DashboardFilters,
    DrilldownResponse,
    ExecutiveDashboardResponse,
    OperationsDashboardResponse,
)
from app.application.use_cases.dashboard import DRILLDOWN_INDICATORS, DashboardUseCase
from app.dependencies import get_dashboard_use_case
from app.domain.services.dashboard_metrics import AGE_BUCKETS, MAX_PERIOD_DAYS
from app.domain.value_objects.document_status import DocumentStatus
from app.infrastructure.config.dashboard_settings import today_local
from app.infrastructure.persistence.repositories.dashboard_repository import (
    BACKLOG_STATUS_BY_STAGE,
)

router = APIRouter(prefix="/dashboard")

_DEFAULT_WINDOW_DAYS = 30


def _invalid(message: str) -> HTTPException:
    return HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, message)


def get_dashboard_filters(
    from_date: Optional[date] = Query(
        None,
        description="Inicio del periodo (inclusive), sobre la fecha de emisión. "
        "Por defecto, 29 días antes de `to_date` (ventana de 30 días).",
    ),
    to_date: Optional[date] = Query(
        None, description="Fin del periodo (inclusive). Por defecto, hoy."
    ),
    receiver_nit: Optional[str] = Query(
        None, description="NIT de la empresa receptora (compradora)."
    ),
    issuer_nit: Optional[str] = Query(None, description="NIT del proveedor emisor."),
    cost_center_id: Optional[int] = Query(None, description="Centro de costo del documento."),
    doc_status: Optional[int] = Query(
        None,
        alias="status",
        description="Estado: 0 Error, 100 Procesado, 200 Causado, 300 Aprobado, 400 Contabilizada.",
    ),
) -> DashboardFilters:
    """Filtros comunes. Valida el rango y el estado: un error del cliente es 422, no 500."""
    to_ = to_date or today_local()
    from_ = from_date or (to_ - timedelta(days=_DEFAULT_WINDOW_DAYS - 1))
    if from_ > to_:
        raise _invalid("`from_date` no puede ser posterior a `to_date`.")
    if (to_ - from_).days + 1 > MAX_PERIOD_DAYS:
        raise _invalid(f"El periodo no puede superar {MAX_PERIOD_DAYS} días.")
    if doc_status is not None and doc_status not in DocumentStatus.ALL:
        raise _invalid(f"`status` inválido. Valores: {sorted(DocumentStatus.ALL)}.")
    return DashboardFilters(
        from_date=from_,
        to_date=to_,
        receiver_nit=(receiver_nit or "").strip() or None,
        issuer_nit=(issuer_nit or "").strip() or None,
        cost_center_id=cost_center_id,
        status=doc_status,
    )


@router.get(
    "/executive",
    response_model=ExecutiveDashboardResponse,
    status_code=status.HTTP_200_OK,
    summary="Dashboard ejecutivo: KPIs del periodo contra el periodo anterior",
    description=(
        "Tarjetas de indicadores clave del periodo. Cada una trae valor actual, valor del "
        "periodo anterior comparable (misma duración, inmediatamente anterior), variación %, "
        "meta y semáforo.\n\n"
        "**Universo:** documentos cuya fecha de emisión cae en el periodo, clasificados por su "
        "estado ACTUAL. No hay historial de transiciones, así que no se puede decir «en el "
        "periodo se contabilizaron N», sino «de los emitidos en el periodo, N están hoy "
        "contabilizados».\n\n"
        "**Nulos:** `value = null` significa que no hay base (p. ej. 0 documentos), no cero. "
        "`variation_pct = null` si el periodo anterior es 0 o no tiene dato.\n\n"
        "**Metas:** configurables con `DASHBOARD_TARGETS` (ver `docs/dashboard-indicadores.md`); "
        "sin meta, `light = null`. Los indicadores `neutral` nunca llevan semáforo.\n\n"
        "**Drill-down:** `drilldown` indica el valor de `indicator` para "
        "`GET /dashboard/documents` que lista los documentos detrás de la cifra.\n\n"
        "Disponible para cualquier usuario autenticado del cliente (lectura)."
    ),
    response_description="Lista de KPIs con comparación, meta y semáforo.",
    responses={422: {"description": "Rango de fechas inválido o `status` desconocido."}},
)
def executive_dashboard(
    filters: DashboardFilters = Depends(get_dashboard_filters),
    use_case: DashboardUseCase = Depends(get_dashboard_use_case),
):
    return use_case.executive(filters)


@router.get(
    "/operations",
    response_model=OperationsDashboardResponse,
    status_code=status.HTTP_200_OK,
    summary="Dashboard de operación: estados, embudo, tendencia y backlog",
    description=(
        "- `by_status`: conteo e importe por estado (siempre los cinco, aunque estén en 0).\n"
        "- `funnel`: embudo acumulativo procesados → causados → aprobados → contabilizados, "
        "con % del total y conversión desde la etapa anterior. «Descargados» no existe: no se "
        "guarda cuándo se descargó un documento.\n"
        "- `series`: documentos e importe por día/semana/mes de emisión (semana = lunes a "
        "domingo). Omite buckets vacíos. `trend` compara los dos últimos buckets completos.\n"
        "- `backlog`: pendientes por etapa y antigüedad (0-3, 4-7, 8-15, 15+ días desde la "
        "emisión). **No usa el rango de fechas**: es lo que está pendiente hoy; sí aplica los "
        "demás filtros.\n\n"
        "**No incluye** tiempo de ciclo ni tasa de automatización (STP): requieren la fecha de "
        "cada transición, que hoy no se registra."
    ),
    response_description="Estados, embudo, serie temporal y backlog.",
    responses={422: {"description": "Rango de fechas inválido o `status` desconocido."}},
)
def operations_dashboard(
    granularity: Literal["day", "week", "month"] = Query(
        "day", description="Agrupación de la serie temporal."
    ),
    filters: DashboardFilters = Depends(get_dashboard_filters),
    use_case: DashboardUseCase = Depends(get_dashboard_use_case),
):
    return use_case.operations(filters, granularity)


@router.get(
    "/documents",
    response_model=DrilldownResponse,
    status_code=status.HTTP_200_OK,
    summary="Drill-down: documentos detrás de un indicador",
    description=(
        "Lista paginada de los documentos que componen un indicador, con los mismos filtros "
        "que las vistas. `total` coincide con la cifra del indicador (o de la celda del "
        "backlog) calculada con los mismos filtros.\n\n"
        "`indicator`: `documents`, `accounted`, `pending_approval`, `approved_not_accounted`, "
        "`in_error`, `without_support`, `accounted_outside_period`, `backlog`.\n\n"
        "Con `indicator=backlog` es obligatorio `stage` (`processed`, `caused`, `approved`, "
        "`error`) y `age_range` es opcional (`0-3`, `4-7`, `8-15`, `15+`); ignora el rango de "
        "fechas y ordena del más antiguo al más reciente. `stage` y `age_range` con otro "
        "indicador son un error.\n\n"
        "Es un GET con filtros en query (no un POST de búsqueda) porque los parámetros son "
        "pocos y simples."
    ),
    response_description="Página de documentos, total y suma de importes de todo el conjunto.",
    responses={
        422: {
            "description": "Indicador desconocido, `stage`/`age_range` inválidos o mal "
            "combinados, o filtros inválidos."
        }
    },
)
def dashboard_documents(
    indicator: str = Query(..., description="Indicador a desglosar. Ver lista en la descripción."),
    stage: Optional[str] = Query(None, description="Solo con `indicator=backlog`."),
    age_range: Optional[str] = Query(
        None, description="Solo con `indicator=backlog`: `0-3`, `4-7`, `8-15` o `15+`."
    ),
    limit: int = Query(50, ge=1, le=500, description="Tamaño de página."),
    offset: int = Query(0, ge=0, description="Registros a omitir."),
    filters: DashboardFilters = Depends(get_dashboard_filters),
    use_case: DashboardUseCase = Depends(get_dashboard_use_case),
):
    if indicator not in DRILLDOWN_INDICATORS:
        raise _invalid(f"`indicator` inválido. Valores: {list(DRILLDOWN_INDICATORS)}.")
    if indicator == "backlog":
        if stage not in BACKLOG_STATUS_BY_STAGE:
            raise _invalid(
                f"`stage` es obligatorio con backlog. Valores: {list(BACKLOG_STATUS_BY_STAGE)}."
            )
        ranges = [label for label, _, _ in AGE_BUCKETS]
        if age_range is not None and age_range not in ranges:
            raise _invalid(f"`age_range` inválido. Valores: {ranges}.")
    elif stage is not None or age_range is not None:
        raise _invalid("`stage` y `age_range` solo aplican con `indicator=backlog`.")

    return use_case.drilldown(
        filters, indicator, stage=stage, age_range=age_range, limit=limit, offset=offset
    )
