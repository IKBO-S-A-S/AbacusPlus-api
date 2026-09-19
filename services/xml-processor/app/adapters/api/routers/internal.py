import os
from typing import Optional

from fastapi import APIRouter, Depends, Header, HTTPException
from sqlalchemy import NullPool, create_engine, text
from sqlalchemy.orm import Session

from app.application.dto.document import (
    DocumentDetailCodeUpdateItem,
    DocumentDetailCodeUpdateResponse,
    DocumentResponse,
)
from app.infrastructure.config.tenant_connection_manager import get_session_for_tenant
from app.infrastructure.persistence.repositories.document_repository import DocumentRepository

router = APIRouter()


def _verify_internal_secret(x_internal_secret: str = Header(...)):
    expected = os.environ.get("INTERNAL_SECRET", "")
    if not expected or x_internal_secret != expected:
        raise HTTPException(status_code=403, detail="Forbidden")


def _get_tenant_db_internal(x_tenant_slug: str = Header(...)) -> Session:
    return get_session_for_tenant(x_tenant_slug)


def _migrate_tenant_db(engine) -> None:
    """Idempotent schema migrations. Safe to run on new or existing tenant DBs."""
    from app.infrastructure.config.database import Base

    Base.metadata.create_all(bind=engine, checkfirst=True)

    with engine.connect() as conn:
        conn.execute(
            text("""
            INSERT INTO document_statuses (id, name) VALUES
                (0, 'Error'), (100, 'Procesado'), (200, 'Causado'),
                (300, 'Aprobado'), (400, 'Contabilizada')
            ON CONFLICT (id) DO NOTHING
        """)
        )
        conn.execute(
            text("""
            DO $$
            BEGIN
                IF (SELECT data_type FROM information_schema.columns
                    WHERE table_name='documents' AND column_name='status') = 'character varying' THEN
                    ALTER TABLE documents ALTER COLUMN status TYPE INTEGER USING
                        CASE status
                            WHEN 'Procesado'      THEN 100
                            WHEN 'procesado'      THEN 100
                            WHEN 'processed'      THEN 100
                            WHEN 'Causado'        THEN 200
                            WHEN 'causado'        THEN 200
                            WHEN 'Aprobado'       THEN 300
                            WHEN 'aprobado'       THEN 300
                            WHEN 'Contabilizada'  THEN 400
                            WHEN 'contabilizada'  THEN 400
                            ELSE 0
                        END;
                END IF;
            END $$;
        """)
        )
        conn.execute(
            text(
                "ALTER TABLE processing_logs " "ADD COLUMN IF NOT EXISTS xml_filename VARCHAR(255)"
            )
        )
        conn.execute(
            text(
                "ALTER TABLE processing_logs "
                "ADD COLUMN IF NOT EXISTS accounting_status VARCHAR(20)"
            )
        )
        conn.execute(
            text("ALTER TABLE processing_logs " "ADD COLUMN IF NOT EXISTS accounting_error TEXT")
        )
        conn.execute(
            text(
                "ALTER TABLE documents "
                "ALTER COLUMN issuer_phone TYPE VARCHAR(100), "
                "ALTER COLUMN receiver_phone TYPE VARCHAR(100)"
            )
        )
        conn.execute(
            text("ALTER TABLE issuers " "ADD COLUMN IF NOT EXISTS tipo_contribuyente VARCHAR(50)")
        )
        # Refactor contable: reemplazar asiento por asignación de cuentas por ítem
        conn.execute(
            text("ALTER TABLE documents DROP COLUMN IF EXISTS accounting_entry_id")
        )
        conn.execute(
            text(
                "ALTER TABLE documents "
                "ADD COLUMN IF NOT EXISTS payment_type_id INTEGER "
                "REFERENCES integration_payment_types(id)"
            )
        )
        conn.execute(
            text("ALTER TABLE document_details ADD COLUMN IF NOT EXISTS code VARCHAR(50)")
        )
        conn.execute(
            text(
                "ALTER TABLE document_details "
                "ADD COLUMN IF NOT EXISTS type VARCHAR(20) NOT NULL DEFAULT 'Account'"
            )
        )
        conn.execute(
            text(
                "ALTER TABLE document_details "
                "ADD COLUMN IF NOT EXISTS tax_id INTEGER "
                "REFERENCES integration_taxes(id)"
            )
        )
        conn.execute(
            text(
                "ALTER TABLE document_details "
                "ADD COLUMN IF NOT EXISTS cost_center_id INTEGER "
                "REFERENCES integration_cost_centers(id)"
            )
        )
        conn.execute(
            text(
                "ALTER TABLE documents "
                "ADD COLUMN IF NOT EXISTS pdf_storage_locations JSONB NOT NULL DEFAULT '{}'"
            )
        )
        conn.execute(
            text(
                "ALTER TABLE documents "
                "ADD COLUMN IF NOT EXISTS xml_storage_locations JSONB NOT NULL DEFAULT '{}'"
            )
        )
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
    "/internal/documents/{document_id}/full",
    include_in_schema=False,
    dependencies=[Depends(_verify_internal_secret)],
)
def get_document_full_internal(
    document_id: int,
    db: Session = Depends(_get_tenant_db_internal),
):
    repo = DocumentRepository(db)
    doc = repo.get_by_id(document_id)
    if doc is None:
        raise HTTPException(status_code=404, detail=f"Document {document_id} not found")
    return DocumentResponse.model_validate(doc, from_attributes=True)


@router.patch(
    "/internal/documents/{document_id}/details",
    include_in_schema=False,
    dependencies=[Depends(_verify_internal_secret)],
)
def update_detail_codes_internal(
    document_id: int,
    assignments: list[DocumentDetailCodeUpdateItem],
    db: Session = Depends(_get_tenant_db_internal),
):
    repo = DocumentRepository(db)
    if repo.get_by_id(document_id) is None:
        raise HTTPException(status_code=404, detail=f"Document {document_id} not found")
    updated = repo.update_detail_codes([a.model_dump() for a in assignments])
    return DocumentDetailCodeUpdateResponse(updated=updated)
