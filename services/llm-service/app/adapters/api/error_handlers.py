import logging

from fastapi import Request
from fastapi.responses import JSONResponse

from app.domain.exceptions.base import DomainException

logger = logging.getLogger(__name__)

STATUS_MAP = {
    "VALIDATION_ERROR": 400,
    # RF-08: falta un prerequisito de negocio (el PUC o el catálogo de retenciones), no es
    # un fallo del servidor: el usuario debe cargar el catálogo antes de pedir sugerencias.
    "NO_CHART_OF_ACCOUNTS": 409,
    "NO_RETENTION_CATALOG": 409,
    # Configuración del despliegue (falta o es inválida la API key de OpenAI): el servicio
    # no puede atender la petición ahora mismo, y no es culpa del cliente.
    "AI_NOT_CONFIGURED": 503,
    "AI_AUTHENTICATION_FAILED": 503,
    "DOMAIN_ERROR": 500,
}


async def domain_exception_handler(request: Request, exc: DomainException) -> JSONResponse:
    return JSONResponse(
        status_code=STATUS_MAP.get(exc.code, 500),
        content={"error": exc.code, "detail": exc.message},
    )


async def unhandled_exception_handler(request: Request, exc: Exception) -> JSONResponse:
    logger.error("Unhandled exception: %s", str(exc), exc_info=True)
    return JSONResponse(
        status_code=500,
        content={"error": "INTERNAL_ERROR", "detail": "An unexpected error occurred"},
    )
