import logging
import os
from contextlib import asynccontextmanager

from fastapi import FastAPI

from app.adapters.api.error_handlers import domain_exception_handler, unhandled_exception_handler
from app.adapters.api.routers.chart_accounts import router as chart_accounts_router
from app.adapters.api.routers.cost_centers import router as cost_centers_router
from app.adapters.api.routers.credentials import router as credentials_router
from app.adapters.api.routers.fiscal_diagnosis import router as fiscal_diagnosis_router
from app.adapters.api.routers.fiscal_profile import router as fiscal_profile_router
from app.adapters.api.routers.internal import _migrate_tenant_db
from app.adapters.api.routers.internal import router as internal_router
from app.adapters.api.routers.payment_types import router as payment_types_router
from app.adapters.api.routers.products import router as products_router
from app.adapters.api.routers.purchase_invoice_parameters import (
    router as purchase_invoice_parameters_router,
)
from app.adapters.api.routers.retention_criteria import router as retention_criteria_router
from app.adapters.api.routers.retentions import router as retentions_router
from app.adapters.api.routers.taxes import router as taxes_router
from app.adapters.api.routers.storage import router as storage_router
from app.domain.exceptions.base import DomainException
from app.infrastructure.config.database import engine
from app.infrastructure.config.logging import setup_logging
from app.infrastructure.persistence.models import (
    chart_account as _chart_account_model,  # noqa: F401
)
from app.infrastructure.persistence.models import cost_center as _cost_center_model  # noqa: F401
from app.infrastructure.persistence.models import integration as _integration_model  # noqa: F401
from app.infrastructure.persistence.models import payment_type as _payment_type_model  # noqa: F401
from app.infrastructure.persistence.models import product as _product_model  # noqa: F401
from app.infrastructure.persistence.models import (
    purchase_invoice_parameter as _purchase_param_model,  # noqa: F401
)
from app.infrastructure.persistence.models import (
    retention_criteria as _retention_criteria_model,  # noqa: F401
)
from app.infrastructure.persistence.models import retention as _retention_model  # noqa: F401
from app.infrastructure.persistence.models import tax as _tax_model  # noqa: F401
from app.infrastructure.persistence.models import (
    tenant_fiscal_profile as _tenant_fiscal_profile_model,  # noqa: F401
)
from app.infrastructure.persistence.models import storage as _storage_model  # noqa: F401

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
app.include_router(fiscal_profile_router, prefix="/api/v1", tags=["integrations"])
app.include_router(fiscal_diagnosis_router, prefix="/api/v1", tags=["integrations"])
app.include_router(payment_types_router, prefix="/api/v1", tags=["integrations"])
app.include_router(retention_criteria_router, prefix="/api/v1", tags=["integrations"])
app.include_router(retentions_router, prefix="/api/v1", tags=["integrations"])
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
