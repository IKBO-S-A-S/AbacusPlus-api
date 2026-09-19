from typing import Any, Optional

from pydantic import BaseModel, Field


class DocumentsRangeRequest(BaseModel):
    token: str
    pk: str = Field("", description="Partner key del portal DIAN (ej. '10910094|1125638394'). Usa EXTERNAL_FIXED_PK del entorno si está vacío.")
    rk: str = Field("", description="Representative key del portal DIAN (ej. '901031352'). Usa EXTERNAL_FIXED_RK del entorno si está vacío.")
    StartDate: str
    EndDate: str
    draw: int = 1
    start: int = 0
    length: int = 100
    DocumentKey: str = ""
    SerieAndNumber: str = ""
    SenderCode: str = ""
    ReceiverCode: str = ""
    DocumentTypeId: str = "00"
    Status: str = "0"
    IsNextPage: bool = False
    FilterType: str = "3"
    blockIndex: int = 0
    RadianStatus: str = "0"


class EnqueueDownloadsResponse(BaseModel):
    batch_id: str = Field(
        ...,
        description="Identificador único del batch. Úsalo en GET /dian/documents/batches/{batch_id} para consultar el estado.",
    )
    enqueued: int = Field(..., description="Número de documentos encolados para descarga.")
    job_ids: list[str] = Field(..., description="IDs individuales de cada job ARQ.")
    StartDate: str
    EndDate: str
    started_at: str = Field(..., description="Fecha y hora de inicio del batch (ISO 8601 UTC).")


class DownloadJobStatus(BaseModel):
    job_id: str
    status: str
    result: Optional[dict[str, Any]] = None


# ── Detalle de progreso por paso ───────────────────────────────────────────────


class StepDownloaded(BaseModel):
    done: bool
    at: Optional[str] = None


class StepXmlProcessed(BaseModel):
    done: bool
    at: Optional[str] = None
    status: Optional[str] = Field(None, description="added | duplicate | error")
    document_id: Optional[int] = None
    error: Optional[str] = None


class StepAccounting(BaseModel):
    done: bool
    at: Optional[str] = None
    status: Optional[str] = Field(None, description="triggered | error")
    error: Optional[str] = None


class JobSteps(BaseModel):
    downloaded: StepDownloaded
    xml_processed: StepXmlProcessed
    accounting: StepAccounting


class JobProgressDetail(BaseModel):
    job_id: str
    track_id: str
    current_step: str = Field(
        ..., description="downloaded | xml_processed | accounting | done | error"
    )
    steps: JobSteps


# ── Resumen por paso del batch ─────────────────────────────────────────────────


class StepError(BaseModel):
    job_id: str = Field(..., description="Track ID del documento que falló en esta etapa.")
    error: str = Field(..., description="Mensaje de error capturado para esta etapa.")


class StepSummary(BaseModel):
    done: int
    pending: int
    error: int
    errors: list[StepError] = Field(
        default_factory=list,
        description="Detalle de los errores de esta etapa (job_id + mensaje). Siempre presente, sin necesitar ?detail=true.",
    )


class BatchStepSummary(BaseModel):
    downloaded: StepSummary
    xml_processed: StepSummary
    accounting: StepSummary


# ── Respuesta del batch ────────────────────────────────────────────────────────


class BatchStatusResponse(BaseModel):
    batch_id: str = Field(..., description="Identificador del batch.")
    total: int = Field(..., description="Total de documentos encolados en el batch.")
    elapsed_seconds: float = Field(..., description="Segundos transcurridos desde el inicio.")
    total_time_seconds: Optional[float] = Field(
        None, description="Tiempo total. Solo presente cuando el batch está completado."
    )
    is_done: bool = Field(
        ..., description="True si todos los jobs completaron sus 3 pasos (con éxito o error)."
    )
    started_at: str = Field(..., description="Fecha y hora de inicio (ISO 8601 UTC).")
    summary: BatchStepSummary = Field(..., description="Conteo de jobs por estado en cada etapa.")
    auth_error: Optional[str] = Field(
        None,
        description=(
            "Presente cuando todos los jobs fallaron por error de autenticación con el portal DIAN "
            "(token vencido, Cloudflare bloqueó el navegador, etc.). "
            "Describe la causa específica para que puedas corregirla antes de reintentar."
        ),
    )
    jobs: Optional[list[JobProgressDetail]] = Field(
        None, description="Detalle por job. Solo presente cuando se solicita con ?detail=true."
    )
