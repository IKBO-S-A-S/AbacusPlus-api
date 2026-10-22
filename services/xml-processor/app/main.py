import asyncio
import logging
import os
from contextlib import asynccontextmanager

from fastapi import FastAPI

from app.adapters.api.error_handlers import domain_exception_handler, unhandled_exception_handler
from app.adapters.api.routers.batch import router as batch_router
from app.adapters.api.routers.catalog import router as catalog_router
from app.adapters.api.routers.dashboard import router as dashboard_router
from app.adapters.api.routers.document_taxes import router as document_taxes_router
from app.adapters.api.routers.documents import router as documents_router
from app.adapters.api.routers.internal import router as internal_router
from app.adapters.api.routers.issuers import router as issuers_router
from app.adapters.api.routers.receivers import router as receivers_router
from app.adapters.api.routers.xml import router as xml_router
from app.domain.exceptions.base import DomainException
from app.infrastructure.config.database import engine
from app.infrastructure.config.logging import setup_logging
from app.infrastructure.persistence.models import (  # noqa: F401
    concept,
    cost_center,
    document,
    document_tax,
    integration_cost_center,
    integration_payment_type,
    integration_tax,
    issuer,
    processing_log,
    puc,
    receiver,
    retention_fuente,
    retention_ica,
    tax,
)
from app.infrastructure.persistence.models.document import DocumentStatus  # noqa: F401
from app.infrastructure.persistence.tenant_migrations import apply_tenant_migrations
from app.infrastructure.queue.accounting_supervisor import accounting_queue_supervisor
from app.infrastructure.queue.download_queue import process_queue_worker

setup_logging()
logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    internal_secret = os.environ.get("INTERNAL_SECRET", "")
    if not internal_secret or internal_secret == "change-me":
        raise RuntimeError(
            "INTERNAL_SECRET no está configurado (o sigue en 'change-me' de .env.example). "
            "Los endpoints /internal/* de todos los servicios lo requieren para autenticar "
            "llamadas entre microservicios. Genera uno real: openssl rand -hex 32"
        )
    # La base por defecto se migra con la MISMA lista que las bases de cada cliente.
    # Antes este bloque repetía un subconjunto de las sentencias, así que el entorno de
    # desarrollo podía quedar con un esquema distinto al de producción y los errores solo
    # aparecían al desplegar.
    aplicadas = apply_tenant_migrations(engine, create_tables=True, strict=False)
    logger.info("Migraciones de esquema aplicadas: %d", aplicadas)
    logger.info("Tablas verificadas/creadas")
    task = asyncio.create_task(process_queue_worker())
    logger.info("Worker de cola iniciado")
    # RF-05: la contabilización dejó de ocurrir dentro de la petición del usuario. Este
    # supervisor es quien la ejecuta ahora, y sin él los documentos se encolarían sin que
    # nadie los enviara nunca.
    accounting_task = asyncio.create_task(accounting_queue_supervisor())
    logger.info("Supervisor de contabilización iniciado")
    yield
    task.cancel()
    accounting_task.cancel()
    logger.info("Worker de cola detenido")


app = FastAPI(
    title="XML Processor Service",
    description="Microservicio para procesar facturas DIAN en formato XML/ZIP",
    version="1.0.0",
    lifespan=lifespan,
)

app.add_exception_handler(DomainException, domain_exception_handler)
app.add_exception_handler(Exception, unhandled_exception_handler)

app.include_router(xml_router, prefix="/api/v1", tags=["xml"])
app.include_router(documents_router, prefix="/api/v1", tags=["documents"])
app.include_router(document_taxes_router, prefix="/api/v1", tags=["document-taxes"])
app.include_router(receivers_router, prefix="/api/v1", tags=["receivers"])
app.include_router(issuers_router, prefix="/api/v1", tags=["issuers"])
app.include_router(catalog_router, prefix="/api/v1", tags=["catalog"])
app.include_router(dashboard_router, prefix="/api/v1", tags=["dashboard"])
app.include_router(batch_router, prefix="/api/v1", tags=["batch"])
app.include_router(internal_router)  # no prefix — path is /internal/provision-tenant

logger.info("XML Processor Service started on port 8001")


@app.get(
    "/health",
    summary="Health check del XML Processor",
    description="Verifica que el microservicio de procesamiento XML esté activo.",
    response_description="Estado operativo del servicio.",
)
async def health_check():
    return {"status": "healthy", "service": "xml-processor"}
