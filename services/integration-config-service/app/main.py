import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI

from app.adapters.api.error_handlers import domain_exception_handler, unhandled_exception_handler
from app.adapters.api.routers.chart_accounts import router as chart_accounts_router
from app.adapters.api.routers.cost_centers import router as cost_centers_router
from app.adapters.api.routers.credentials import router as credentials_router
from app.adapters.api.routers.internal import _migrate_tenant_db, router as internal_router
from app.adapters.api.routers.payment_types import router as payment_types_router
from app.adapters.api.routers.taxes import router as taxes_router
from app.adapters.api.routers.products import router as products_router
from app.adapters.api.routers.purchase_invoice_parameters import (
    router as purchase_invoice_parameters_router,
)
from app.adapters.api.routers.storage import router as storage_router
from app.domain.exceptions.base import DomainException
from app.infrastructure.config.database import engine
from app.infrastructure.config.logging import setup_logging
from app.infrastructure.persistence.models import (
    chart_account as _chart_account_model,  # noqa: F401
)
from app.infrastructure.persistence.models import cost_center as _cost_center_model  # noqa: F401
from app.infrastructure.persistence.models import payment_type as _payment_type_model  # noqa: F401
from app.infrastructure.persistence.models import tax as _tax_model  # noqa: F401
from app.infrastructure.persistence.models import integration as _integration_model  # noqa: F401
from app.infrastructure.persistence.models import product as _product_model  # noqa: F401
from app.infrastructure.persistence.models import (
    purchase_invoice_parameter as _purchase_param_model,  # noqa: F401
)
from app.infrastructure.persistence.models import storage as _storage_model  # noqa: F401

setup_logging()
logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    _migrate_tenant_db(engine)
    logger.info("Tablas de integration-config-service verificadas/creadas")
    yield


app = FastAPI(
    title="Integration Config Service",
    description=(
        "Microservicio central para parametrizar integraciones externas.\n\n"
        "Administra credenciales y parametros reutilizables por proveedor, de modo que "
        "los adaptadores como SIIGO consuman configuracion sin ser responsables de crearla."
    ),
    version="1.0.0",
    lifespan=lifespan,
)

app.add_exception_handler(DomainException, domain_exception_handler)
app.add_exception_handler(Exception, unhandled_exception_handler)

app.include_router(chart_accounts_router, prefix="/api/v1", tags=["integrations"])
app.include_router(cost_centers_router, prefix="/api/v1", tags=["integrations"])
app.include_router(credentials_router, prefix="/api/v1", tags=["integrations"])
app.include_router(payment_types_router, prefix="/api/v1", tags=["integrations"])
app.include_router(taxes_router, prefix="/api/v1", tags=["integrations"])
app.include_router(products_router, prefix="/api/v1", tags=["integrations"])
app.include_router(purchase_invoice_parameters_router, prefix="/api/v1", tags=["integrations"])
app.include_router(storage_router, prefix="/api/v1", tags=["integrations"])
app.include_router(internal_router)  # no prefix — path is /internal/provision-tenant

logger.info("Integration Config Service started on port 8007")


@app.get(
    "/health",
    summary="Health check del Integration Config Service",
    description="Verifica que el microservicio de configuracion de integraciones este activo.",
    response_description="Estado operativo del servicio.",
)
async def health_check():
    return {"status": "healthy", "service": "integration-config-service"}
