import os

from fastapi import APIRouter, Depends, Header, HTTPException
from sqlalchemy import NullPool, create_engine, text

from app.application.dto.storage import StorageSettingsResponse
from app.application.use_cases.manage_storage_settings import ManageStorageSettingsUseCase
from app.infrastructure.config.tenant_connection_manager import get_session_for_tenant
from app.infrastructure.persistence.repositories.storage_repository import (
    DocumentNamingSettingRepository,
    S3StorageConfigRepository,
    SharePointStorageConfigRepository,
)

router = APIRouter()


def _verify_internal_secret(x_internal_secret: str = Header(...)):
    expected = os.environ.get("INTERNAL_SECRET", "")
    if not expected or x_internal_secret != expected:
        raise HTTPException(status_code=403, detail="Forbidden")


def _migrate_tenant_db(engine) -> None:
    """Idempotent schema migrations. Safe to run on new or existing tenant DBs."""
    from app.infrastructure.config.database import Base

    Base.metadata.create_all(bind=engine, checkfirst=True)

    migrations = [
        # integration_cost_centers: drop legacy columns, add constraint, add new columns
        "ALTER TABLE integration_cost_centers DROP COLUMN IF EXISTS provider",
        "ALTER TABLE integration_cost_centers DROP COLUMN IF EXISTS account_key",
        "ALTER TABLE integration_cost_centers DROP CONSTRAINT IF EXISTS uq_cost_center_provider_key_code",
        (
            "DO $$ BEGIN "
            "IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'uq_cost_center_code') "
            "THEN ALTER TABLE integration_cost_centers ADD CONSTRAINT uq_cost_center_code UNIQUE (code); "
            "END IF; END $$"
        ),
        "ALTER TABLE integration_cost_centers ADD COLUMN IF NOT EXISTS external_id VARCHAR(120)",
        "ALTER TABLE integration_cost_centers ADD COLUMN IF NOT EXISTS raw_payload JSONB NOT NULL DEFAULT '{}'",
        "ALTER TABLE integration_cost_centers ADD COLUMN IF NOT EXISTS synced_at TIMESTAMPTZ NOT NULL DEFAULT now()",
        "ALTER TABLE integration_cost_centers ADD COLUMN IF NOT EXISTS created_at TIMESTAMPTZ NOT NULL DEFAULT now()",
        "ALTER TABLE integration_cost_centers ADD COLUMN IF NOT EXISTS updated_at TIMESTAMPTZ NOT NULL DEFAULT now()",
        # integration_chart_accounts
        "ALTER TABLE integration_chart_accounts ADD COLUMN IF NOT EXISTS external_id VARCHAR(120)",
        "ALTER TABLE integration_chart_accounts ADD COLUMN IF NOT EXISTS account_type VARCHAR(80)",
        "ALTER TABLE integration_chart_accounts ADD COLUMN IF NOT EXISTS level INTEGER",
        "ALTER TABLE integration_chart_accounts ADD COLUMN IF NOT EXISTS parent_code VARCHAR(80)",
        "ALTER TABLE integration_chart_accounts ADD COLUMN IF NOT EXISTS accepts_movements BOOLEAN",
        "ALTER TABLE integration_chart_accounts ADD COLUMN IF NOT EXISTS raw_payload JSONB NOT NULL DEFAULT '{}'",
        "ALTER TABLE integration_chart_accounts ADD COLUMN IF NOT EXISTS synced_at TIMESTAMPTZ NOT NULL DEFAULT now()",
        "ALTER TABLE integration_chart_accounts ADD COLUMN IF NOT EXISTS created_at TIMESTAMPTZ NOT NULL DEFAULT now()",
        "ALTER TABLE integration_chart_accounts ADD COLUMN IF NOT EXISTS updated_at TIMESTAMPTZ NOT NULL DEFAULT now()",
        # integration_products
        "ALTER TABLE integration_products ADD COLUMN IF NOT EXISTS raw_payload JSONB NOT NULL DEFAULT '{}'",
        "ALTER TABLE integration_products ADD COLUMN IF NOT EXISTS synced_at TIMESTAMPTZ NOT NULL DEFAULT now()",
        "ALTER TABLE integration_products ADD COLUMN IF NOT EXISTS created_at TIMESTAMPTZ NOT NULL DEFAULT now()",
        "ALTER TABLE integration_products ADD COLUMN IF NOT EXISTS updated_at TIMESTAMPTZ NOT NULL DEFAULT now()",
        # integration_payment_types
        "ALTER TABLE integration_payment_types ADD COLUMN IF NOT EXISTS created_at TIMESTAMPTZ NOT NULL DEFAULT now()",
        "ALTER TABLE integration_payment_types ADD COLUMN IF NOT EXISTS updated_at TIMESTAMPTZ NOT NULL DEFAULT now()",
        # integration_taxes
        "ALTER TABLE integration_taxes ADD COLUMN IF NOT EXISTS created_at TIMESTAMPTZ NOT NULL DEFAULT now()",
        "ALTER TABLE integration_taxes ADD COLUMN IF NOT EXISTS updated_at TIMESTAMPTZ NOT NULL DEFAULT now()",
    ]

    with engine.connect() as conn:
        for sql in migrations:
            conn.execute(text(sql))
        conn.commit()


@router.post(
    "/internal/provision-tenant",
    include_in_schema=False,
    dependencies=[Depends(_verify_internal_secret)],
)
def provision_tenant(tenant_slug: str):
    """Create/migrate all tables for this service in the tenant DB. Safe to re-run on existing tenants."""
    user = os.environ["DATABASE_USER"]
    password = os.environ["DATABASE_PASSWORD"]
    host = os.environ["DATABASE_HOST"]
    port = os.environ.get("DATABASE_PORT", "5432")
    url = f"postgresql+psycopg2://{user}:{password}@{host}:{port}/abacus_t_{tenant_slug}"
    engine = create_engine(url, poolclass=NullPool)
    _migrate_tenant_db(engine)
    engine.dispose()
    return {
        "status": "provisioned",
        "tenant_slug": tenant_slug,
        "service": os.environ.get("SERVICE_NAME", "unknown"),
    }


@router.get(
    "/internal/storage-settings",
    response_model=StorageSettingsResponse,
    include_in_schema=False,
    dependencies=[Depends(_verify_internal_secret)],
)
def get_internal_storage_settings(tenant_slug: str) -> StorageSettingsResponse:
    """Configuracion agregada de almacenamiento (S3/SharePoint/patron de nombre) con
    secretos desencriptados, para consumo servicio-a-servicio (xml-processor). No requiere
    JWT de usuario — se autentica con X-Internal-Secret, igual que provision-tenant, porque
    el worker de background que la consume no tiene un token de usuario disponible."""
    db = get_session_for_tenant(tenant_slug)
    try:
        use_case = ManageStorageSettingsUseCase(
            s3_repository=S3StorageConfigRepository(db),
            sharepoint_repository=SharePointStorageConfigRepository(db),
            naming_repository=DocumentNamingSettingRepository(db),
        )
        return use_case.get_settings()
    finally:
        db.close()
