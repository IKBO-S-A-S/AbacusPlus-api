import logging

from fastapi import Request
from fastapi.responses import JSONResponse

from app.domain.exceptions.base import DomainException

logger = logging.getLogger(__name__)

STATUS_MAP = {
    "VALIDATION_ERROR": 400,
    "SESSION_NOT_FOUND": 404,
    "SESSION_EXPIRED": 401,
    "EXTERNAL_AUTH_ERROR": 401,
    "EXTERNAL_REQUEST_ERROR": 502,
    "RK_NIT_MISMATCH": 422,
    "FISCAL_NIT_NOT_CONFIGURED": 422,
    "DOMAIN_ERROR": 500,
}


async def domain_exception_handler(request: Request, exc: DomainException) -> JSONResponse:
    status_code = STATUS_MAP.get(exc.code, 500)
    return JSONResponse(
        status_code=status_code,
        content={"error": exc.code, "detail": exc.message},
    )


async def unhandled_exception_handler(request: Request, exc: Exception) -> JSONResponse:
    logger.error("Excepción no manejada: %s", str(exc), exc_info=True)
    return JSONResponse(
        status_code=500,
        content={"error": "INTERNAL_ERROR", "detail": "An unexpected error occurred"},
    )
