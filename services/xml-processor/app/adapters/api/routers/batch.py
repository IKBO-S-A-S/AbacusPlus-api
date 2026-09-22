from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query, status

from app.application.dto.batch import (
    EnqueueBatchResponse,
    ProcessFileRequest,
    ProcessingLogResponse,
)
from app.application.use_cases.process_downloads import ProcessDownloadsUseCase
from app.application.use_cases.process_single_file import ProcessSingleFileUseCase
from app.dependencies import (
    get_process_downloads_use_case,
    get_process_single_file_use_case,
    get_processing_log_repo,
)
from app.infrastructure.config.auth_dependency import require_internal_secret, require_write
from app.infrastructure.persistence.repositories.processing_log_repository import (
    ProcessingLogRepository,
)

router = APIRouter()


@router.post(
    "/batch-jobs/downloads",
    dependencies=[Depends(require_write)],
    response_model=EnqueueBatchResponse,
    status_code=202,
    summary="Procesar descargas pendientes",
    description=(
        "Escanea la carpeta configurada en `DOWNLOADS_DIR` (por defecto `/app/downloads`), "
        "encola cada archivo `.zip` encontrado y retorna inmediatamente. "
        "El procesamiento ocurre en segundo plano: cada ZIP se descomprime, se parsea el XML "
        "DIAN, se guarda en la base de datos, se indexa en RAG y se solicita la generación "
        "del asiento contable al servicio LLM.\n\n"
        "Los archivos procesados se mueven a `downloads/processed/`. "
        "Los que generan error se mueven a `downloads/errors/`.\n\n"
        "**Idempotente:** si no hay ZIPs pendientes retorna `enqueued: 0` sin error."
    ),
    response_description="Cantidad de archivos encolados para procesamiento.",
)
async def process_downloads(
    use_case: ProcessDownloadsUseCase = Depends(get_process_downloads_use_case),
):
    return await use_case.execute()


@router.post(
    "/batch-jobs/file",
    dependencies=[Depends(require_internal_secret)],
    response_model=EnqueueBatchResponse,
    status_code=202,
    summary="Procesar un ZIP específico por nombre de archivo",
    description=(
        "Encola el archivo ZIP indicado en `filename` para procesamiento inmediato. "
        "A diferencia de `POST /api/v1/batch-jobs/downloads`, este endpoint opera sobre un único archivo "
        "y asocia el procesamiento al `job_id` del worker que lo descargó, "
        "permitiendo actualizar el progreso en Redis.\n\n"
        "**Uso interno**: lo llama `session-proxy-worker` justo después de bajar cada ZIP de "
        "la DIAN, sin sesión de usuario detrás — se autentica con `X-Internal-Secret`, no con "
        "bearer token, y por eso no pasa por el gateway.\n\n"
        "El archivo debe existir en `DOWNLOADS_DIR`. Si no existe retorna 404."
    ),
    response_description="Confirmación del archivo encolado.",
    responses={
        404: {"description": "El archivo no existe en DOWNLOADS_DIR."},
    },
)
async def process_single_file(
    request: ProcessFileRequest,
    use_case: ProcessSingleFileUseCase = Depends(get_process_single_file_use_case),
):
    result = await use_case.execute(request.filename, request.job_id, request.tenant_slug)
    if result is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Archivo '{request.filename}' no encontrado en DOWNLOADS_DIR.",
        )
    return result


@router.get(
    "/batch-logs",
    response_model=list[ProcessingLogResponse],
    summary="Historial de procesamiento batch",
    description=(
        "Retorna el historial de todos los archivos procesados por el worker batch, "
        "con su estado final y el resultado de la solicitud de asiento contable al LLM.\n\n"
        "**Estados posibles (`status`):**\n"
        "- `added` — documento procesado y guardado correctamente.\n"
        "- `duplicate` — el número de documento ya existía en la BD.\n"
        "- `error` — ocurrió un error durante el procesamiento.\n\n"
        "**Estados de causación (`accounting_status`):**\n"
        "- `triggered` — el LLM recibió la solicitud y respondió con éxito.\n"
        "- `error` — la solicitud al LLM falló (ver `accounting_error`).\n"
        "- `null` — no aplica (documento duplicado o con error de procesamiento).\n\n"
        "**Estados de publicación de archivos (`storage_status`, RF-03):**\n"
        "- `ok` — el PDF/XML se subió a todos los backends configurados del tenant (S3 y/o SharePoint).\n"
        "- `error` — algún backend falló al publicar (ver `storage_error`, p. ej. una caída de "
        "conexión con S3 o SharePoint).\n"
        "- `null` — no aplica (aún no había PDF/XML que publicar)."
    ),
    response_description="Lista de registros de procesamiento, ordenados del más reciente al más antiguo.",
)
async def get_processing_logs(
    status: Optional[str] = Query(
        None,
        description="Filtrar por estado: `added`, `duplicate` o `error`.",
    ),
    storage_status: Optional[str] = Query(
        None,
        description="Filtrar por resultado de publicación en S3/SharePoint: `ok` o `error`.",
    ),
    log_repo: ProcessingLogRepository = Depends(get_processing_log_repo),
):
    return log_repo.get_all(status=status, storage_status=storage_status)
