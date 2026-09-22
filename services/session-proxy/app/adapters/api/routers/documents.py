from fastapi import APIRouter, Depends, HTTPException, Query, status

from app.application.dto.documents import (
    BatchStatusResponse,
    DocumentsRangeRequest,
    DownloadJobStatus,
    EnqueueDownloadsResponse,
)
from app.application.use_cases.fetch_and_enqueue_documents import FetchAndEnqueueDocumentsUseCase
from app.application.use_cases.get_batch_status import GetBatchStatusUseCase
from app.application.use_cases.get_job_status import GetJobStatusUseCase
from app.dependencies import (
    get_batch_status_use_case,
    get_fetch_and_enqueue_use_case,
    get_job_status_use_case,
)
from app.domain.exceptions.base import ExternalAuthException, ExternalRequestException
from app.infrastructure.config.auth_dependency import TokenData, get_token_data, require_write

router = APIRouter(dependencies=[Depends(get_token_data)])


@router.post(
    "/dian/downloads",
    dependencies=[Depends(require_write)],
    response_model=EnqueueDownloadsResponse,
    status_code=202,
    summary="Consulta documentos DIAN por rango de fechas y encola descarga de ZIPs",
    description=(
        "Consulta el portal DIAN por rango de fechas, filtra documentos con `DocumentTypeId=96` "
        "y encola la descarga de cada ZIP en segundo plano.\n\n"
        "Retorna un `batch_id` que puedes usar en "
        "`GET /api/v1/dian/documents/batches/{batch_id}` para monitorear el progreso."
    ),
    response_description="Batch creado con su ID, cantidad de jobs encolados y hora de inicio.",
)
async def enqueue_document_downloads(
    request: DocumentsRangeRequest,
    token: TokenData = Depends(get_token_data),
    use_case: FetchAndEnqueueDocumentsUseCase = Depends(get_fetch_and_enqueue_use_case),
):
    try:
        return await use_case.execute(request, token.tenant_slug)
    except ExternalAuthException as e:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail=e.message)
    except ExternalRequestException as e:
        raise HTTPException(status_code=status.HTTP_502_BAD_GATEWAY, detail=e.message)


@router.get(
    "/dian/documents/batches/{batch_id}",
    response_model=BatchStatusResponse,
    summary="Consultar estado de un batch de descarga",
    description=(
        "Retorna el estado actual del batch con un resumen de progreso por etapa:\n\n"
        "- **downloaded** — ZIP descargado del portal DIAN.\n"
        "- **xml_processed** — XML parseado y guardado en base de datos.\n"
        "- **accounting** — Asiento contable generado por el LLM.\n\n"
        "`summary.<etapa>.errors` trae, para cada documento fallido, un `error_code` "
        "específico además del mensaje — el de `downloaded` distingue si el documento "
        "**no existe en la DIAN** (`DIAN_INVALID`, no reintentable), si el **token venció** "
        "(`AUTH_FAILED`/`AUTH_LOST`, hace falta un enlace nuevo) o si fue una **caída "
        "transitoria** (`FETCH_ERROR`/`CLOUDFLARE`/`AZURE_WAF`, reintentable). Un fallo de "
        "descarga se reporta únicamente en `downloaded` — nunca aparece también en "
        "`xml_processed`, ya que el XML nunca llegó a procesarse.\n\n"
        "Cuando `is_done` es `true` se incluye `total_time_seconds` con el tiempo total.\n\n"
        "Usa `?detail=true` para incluir el campo `jobs` con el progreso individual de cada documento."
    ),
    response_description="Estado del batch con resumen por etapa y progreso individual opcional.",
    responses={
        404: {"description": "Batch no encontrado o expirado (TTL: 7 días)."},
    },
)
async def get_batch_status(
    batch_id: str,
    detail: bool = Query(
        False,
        description="Si es true, incluye el detalle de progreso de cada job en el campo `jobs`.",
    ),
    use_case: GetBatchStatusUseCase = Depends(get_batch_status_use_case),
):
    result = await use_case.execute(batch_id, detail=detail)
    if result is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Batch {batch_id} no encontrado o expirado.",
        )
    return result


@router.get(
    "/dian/documents/jobs/{job_id}",
    response_model=DownloadJobStatus,
    summary="Consulta el estado de un job individual de descarga",
    description=(
        "Consulta el progreso de un documento específico dentro del pipeline de descarga y procesamiento.\n\n"
        "El estado indica si el ZIP fue descargado desde DIAN, si el XML ya fue procesado por "
        "`xml-processor` y si se disparó o completó la causación contable."
    ),
    response_description="Estado detallado del job de descarga/procesamiento.",
    responses={
        404: {"description": "Job no encontrado o expirado."},
    },
)
async def get_download_job_status(
    job_id: str,
    use_case: GetJobStatusUseCase = Depends(get_job_status_use_case),
):
    return await use_case.execute(job_id)
