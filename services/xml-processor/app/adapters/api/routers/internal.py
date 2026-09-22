import hmac
import logging
import os

import httpx
from fastapi import APIRouter, Depends, Header, HTTPException
from sqlalchemy import NullPool, create_engine, text
from sqlalchemy.orm import Session

from app.application.dto.catalog import (
    CostCenterProjectionItem,
    CostCenterProjectionResponse,
    PucAccountProjectionItem,
    PucAccountProjectionResponse,
)
from app.application.dto.document import (
    DocumentDetailCodeUpdateItem,
    DocumentDetailCodeUpdateResponse,
    DocumentResponse,
)
from app.application.dto.document_tax import (
    DocumentTaxCreateRequest,
    DocumentTaxSuggestionResponse,
)
from app.domain.services.account_assignment import validate_assignments
from app.domain.services.rag_content import (
    build_accounted_knowledge_content,
    build_accounted_knowledge_metadata,
    build_accounted_knowledge_signature,
)
from app.domain.value_objects.document_status import DocumentStatus
from app.infrastructure.config.tenant_connection_manager import get_session_for_tenant
from app.infrastructure.persistence.models.document import Document
from app.infrastructure.persistence.repositories.cost_center_repository import (
    CostCenterRepository,
)
from app.infrastructure.persistence.repositories.document_repository import DocumentRepository
from app.infrastructure.persistence.repositories.document_tax_repository import (
    DocumentTaxRepository,
)
from app.infrastructure.persistence.repositories.issuer_repository import IssuerRepository
from app.infrastructure.persistence.repositories.puc_repository import PucRepository
from app.infrastructure.persistence.repositories.retention_repository import RetentionRepository
from app.infrastructure.persistence.repositories.tax_or_retention_repository import (
    TaxOrRetentionRepository,
)
from app.infrastructure.persistence.tenant_migrations import apply_tenant_migrations

logger = logging.getLogger(__name__)

router = APIRouter()


def _verify_internal_secret(x_internal_secret: str = Header(...)):
    expected = os.environ.get("INTERNAL_SECRET", "")
    if not expected or not hmac.compare_digest(x_internal_secret, expected):
        raise HTTPException(status_code=403, detail="Forbidden")


def _get_tenant_db_internal(x_tenant_slug: str = Header(...)) -> Session:
    return get_session_for_tenant(x_tenant_slug)


def _migrate_tenant_db(engine) -> None:
    """Lleva la base del cliente al esquema vigente.

    Las sentencias viven en `infrastructure/persistence/tenant_migrations.py`, que es el
    único sitio donde se declaran. Antes estaban copiadas aquí, en `main.py` y en el gestor
    de conexiones, con contenidos distintos: añadir una columna obligaba a acertar en cuál
    de las tres listas, y olvidar una dejaba bases de clientes desactualizadas.

    `strict=True`: al aprovisionar, un fallo debe impedir que el cliente se declare listo
    con el esquema a medias.
    """
    apply_tenant_migrations(engine, create_tables=True, strict=True)
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
    _autoseed_reference_data(tenant_slug)
    return {
        "status": "provisioned",
        "tenant_slug": tenant_slug,
        "service": os.environ.get("SERVICE_NAME", "unknown"),
    }


def _autoseed_reference_data(tenant_slug: str) -> None:
    """Carga datos de referencia nacionales al aprovisionar, solo si aún no existen.

    Es NO destructivo: si el tenant ya tiene tarifas de ReteFuente (cargadas por seed, import
    de Excel o edición del contador), no las toca. Así una entrega o un tenant nuevo arranca
    con la tabla nacional de ReteFuente sin intervención manual, pero re-aprovisionar nunca
    pisa cambios. ReteICA no se auto-carga: es municipal y la ingresa el contador (Excel).
    """
    from app.domain.services.retention_fuente_seed import STANDARD_RETEFUENTE_2026
    from app.infrastructure.persistence.models.retention_fuente import RetentionFuenteRate

    db = get_session_for_tenant(tenant_slug)
    try:
        if db.query(RetentionFuenteRate).count() == 0:
            db.add_all([RetentionFuenteRate(**row) for row in STANDARD_RETEFUENTE_2026])
            db.commit()
            logger.info(
                "Auto-seed ReteFuente tenant=%s: %d tarifas nacionales cargadas",
                tenant_slug,
                len(STANDARD_RETEFUENTE_2026),
            )
    finally:
        db.close()


@router.post(
    "/internal/retention-fuente-rates/seed",
    include_in_schema=False,
    dependencies=[Depends(_verify_internal_secret)],
)
def seed_retention_fuente_rates(tenant_slug: str):
    """Carga la tabla ESTÁNDAR de ReteFuente (nacional) en el tenant. Idempotente.

    Reemplaza el contenido de `retention_fuente_rates` con la semilla vigente
    (`STANDARD_RETEFUENTE_2026`). Habilita que `suggest_retentions` proponga ReteFuente con
    una tarifa verificable por concepto, en vez de suprimirla por falta de tabla oficial.
    """
    from app.domain.services.retention_fuente_seed import STANDARD_RETEFUENTE_2026
    from app.infrastructure.persistence.models.retention_fuente import RetentionFuenteRate

    db = get_session_for_tenant(tenant_slug)
    try:
        db.query(RetentionFuenteRate).delete(synchronize_session=False)
        db.add_all([RetentionFuenteRate(**row) for row in STANDARD_RETEFUENTE_2026])
        db.commit()
        count = db.query(RetentionFuenteRate).count()
    finally:
        db.close()
    logger.info("Seed ReteFuente tenant=%s: %d tarifas cargadas", tenant_slug, count)
    return {"tenant_slug": tenant_slug, "loaded": count}


@router.post(
    "/internal/documents/reindex",
    include_in_schema=False,
    dependencies=[Depends(_verify_internal_secret)],
)
async def reindex_documents(tenant_slug: str):
    """RF-08: reconstruye el conocimiento del RAG a partir de los documentos contabilizados.

    Backfill y reparación en una sola operación. Recorre `documents` de la BD del tenant y
    aplica el criterio de RF-08 documento a documento:

    - **Contabilizado y con id de SIIGO** → se indexa como conocimiento validado. Su causación
      superó todo el flujo, así que puede servir de precedente.
    - **Cualquier otro estado** → se *retira* del RAG. Cubre a la vez los documentos indexados
      por las versiones anteriores (que aprendían al procesar y al aprobar, sin garantía de
      que la factura llegara a SIIGO) y los que dejaron de estar contabilizados por un ajuste
      o una reversión: una causación que ya no refleja un asiento vigente no puede seguir
      usándose como referencia.

    Es idempotente: el alta hace upsert por documento y la retirada no falla si no había nada.
    Ejecutarlo dos veces deja el mismo resultado, y ejecutarlo tras un incidente del
    rag-service repone el conocimiento que se hubiera perdido.
    """
    secret = os.environ.get("INTERNAL_SECRET", "")
    if not secret:
        raise HTTPException(status_code=500, detail="INTERNAL_SECRET no configurado")
    rag_url = os.getenv("RAG_SERVICE_URL", "http://rag-service:8002").rstrip("/")

    db = get_session_for_tenant(tenant_slug)
    indexed = 0
    revoked = 0
    failed = 0
    total = 0
    try:
        # Mapas id -> nombre de los catálogos, para nombrar retenciones y centros de costo.
        # Combinado (impuestos + retenciones): un `tax_id` de `document_taxes` puede resolver
        # en cualquiera de las dos tablas desde la migración del 2026-08-31.
        tax_name_map = {t.id: t.name for t in TaxOrRetentionRepository(db).get_active()}
        cost_center_map = {c.id: c.name for c in CostCenterRepository(db).get_active()}
        # Municipio del tenant, si tiene uno solo configurado (ver el publicador RF-08).
        _ica_codes = {
            str(r.municipality_code or "").strip() for r in RetentionRepository(db).get_ica_rates()
        } - {""}
        municipality_code = _ica_codes.pop() if len(_ica_codes) == 1 else ""
        tax_repo = DocumentTaxRepository(db)
        documents = db.query(Document).all()
        total = len(documents)
        headers = {"X-Internal-Secret": secret}
        async with httpx.AsyncClient(timeout=30.0) as client:
            for doc in documents:
                try:
                    es_conocimiento = doc.status == DocumentStatus.CONTABILIZADA and bool(
                        doc.siigo_id
                    )
                    if not es_conocimiento:
                        resp = await client.post(
                            f"{rag_url}/internal/chunks/revoke",
                            json={
                                "tenant_slug": tenant_slug,
                                "source_type": "invoice",
                                "source_id": doc.id,
                            },
                            headers=headers,
                        )
                        resp.raise_for_status()
                        revoked += 1
                        continue

                    doc_taxes = list(tax_repo.list_by_document(doc.id))
                    content = build_accounted_knowledge_content(
                        document=doc,
                        taxes=doc_taxes,
                        tax_name_map=tax_name_map,
                        siigo_id=doc.siigo_id,
                        siigo_name=getattr(doc, "siigo_name", None),
                        cost_center_name_map=cost_center_map,
                    )
                    # Misma separación que en la indexación normal: se lee el texto completo
                    # y se busca por la firma del caso.
                    embedding_text = build_accounted_knowledge_signature(
                        document=doc,
                        taxes=doc_taxes,
                        tax_name_map=tax_name_map,
                    )
                    metadata = build_accounted_knowledge_metadata(
                        document=doc,
                        taxes=doc_taxes,
                        tax_name_map=tax_name_map,
                        municipality_code=municipality_code,
                    )
                    resp = await client.post(
                        f"{rag_url}/internal/chunks",
                        json={
                            "tenant_slug": tenant_slug,
                            "source_type": "invoice",
                            "source_id": doc.id,
                            "content": content,
                            "embedding_text": embedding_text,
                            "is_validated": True,
                            "siigo_id": doc.siigo_id,
                            "metadata": metadata,
                        },
                        headers=headers,
                    )
                    resp.raise_for_status()
                    indexed += 1
                except Exception as exc:
                    failed += 1
                    logger.warning(
                        "Reindex: falló el documento id=%s (tenant=%s): %s",
                        doc.id,
                        tenant_slug,
                        exc,
                    )
    finally:
        db.close()

    logger.info(
        "Reindex tenant=%s: %d indexados, %d retirados, %d con error, %d total",
        tenant_slug,
        indexed,
        revoked,
        failed,
        total,
    )
    return {
        "tenant_slug": tenant_slug,
        "total": total,
        "indexed": indexed,
        "revoked": revoked,
        "failed": failed,
    }


@router.get(
    "/internal/issuers/{nit}",
    include_in_schema=False,
    dependencies=[Depends(_verify_internal_secret)],
)
def get_issuer_internal(
    nit: str,
    db: Session = Depends(_get_tenant_db_internal),
):
    """RF-08: datos del emisor para el llm-service.

    El `tipo_contribuyente` guarda los códigos de responsabilidad fiscal de la DIAN, que
    son los que determinan qué retenciones proceden sobre el tercero. `documents` almacena
    los datos del emisor desnormalizados y no incluye ese campo, de ahí esta consulta.
    Devuelve 404 si el emisor no está registrado; el llamador lo trata como sin contexto.
    """
    issuer = IssuerRepository(db).get_by_nit(nit)
    if issuer is None:
        raise HTTPException(status_code=404, detail=f"Issuer {nit} not found")
    return {
        "nit": issuer.nit,
        "name": issuer.name,
        "tipo_contribuyente": issuer.tipo_contribuyente,
        "notes": issuer.notes,
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
    """Persiste las cuentas que el llm-service determinó para un documento.

    Aplica exactamente las mismas reglas de dominio que la ruta pública, más la exigencia
    de que la cuenta pueda recibir el ítem de una compra. Sin esta validación el modelo
    llegó a persistir cuentas inexistentes en el catálogo y de clases contablemente
    imposibles: la ruta pública validaba y ésta no, así que la interfaz impedía al contador
    lo que el modelo sí podía hacer.

    Las asignaciones inválidas se descartan y el resto se guarda. Abortar el lote entero
    dejaría sin cuenta a líneas correctas por culpa de una que el modelo erró, y esta ruta
    corre sin nadie escuchando (se dispara al procesar el XML).
    """
    repo = DocumentRepository(db)
    document = repo.get_by_id(document_id)
    if document is None:
        raise HTTPException(status_code=404, detail=f"Document {document_id} not found")

    # Regla más estricta que la de la ruta pública, y a propósito: aquí se usa
    # `is_editable` —solo Procesado y Causado— y NO `is_editable_document`, que además
    # admite los documentos en Error corregibles.
    #
    # La diferencia importa porque quien escribe por esta ruta es el modelo, no una persona.
    # Un documento en Error corregible es uno que SIIGO rechazó y que el contador está a
    # punto de arreglar a mano; dejar que el modelo le reasigne las cuentas en ese momento
    # sobrescribiría la corrección con una nueva propuesta automática, que es justo lo que el
    # contador acaba de descartar. La excepción de RF-05 abre la edición **al usuario**, no a
    # la reasignación automática.
    if not DocumentStatus.is_editable(document.status):
        raise HTTPException(
            status_code=409,
            detail=(
                f"El documento {document_id} está en estado "
                f"'{DocumentStatus.NAMES.get(document.status, document.status)}' y no admite "
                "reasignación de cuentas."
            ),
        )

    outcome = validate_assignments(
        assignments=assignments,
        own_detail_ids={d.id for d in document.details},
        puc_index={
            p.code: {"is_active": p.is_active, "accepts_movements": p.accepts_movements}
            for p in PucRepository(db).get_active()
        },
        valid_cost_center_ids={c.id for c in CostCenterRepository(db).get_active()},
        enforce_item_class=True,
    )

    for rejection in outcome.rejected:
        logger.warning(
            "Sugerencia descartada (doc=%s, linea=%s, cuenta=%s): %s",
            document_id,
            rejection.detail_id,
            rejection.code,
            rejection.reason,
        )

    # RF-04: esta ruta interna solo la consume el llm-service, así que las cuentas que
    # llegan por aquí quedan marcadas como sugeridas por el modelo.
    updated = repo.update_detail_codes(
        [a.model_dump(exclude_unset=True) for a in outcome.accepted], code_source="llm"
    )
    return DocumentDetailCodeUpdateResponse(
        updated=updated, rejected=[r.reason for r in outcome.rejected]
    )


@router.post(
    "/internal/documents/{document_id}/taxes",
    include_in_schema=False,
    dependencies=[Depends(_verify_internal_secret)],
)
def create_document_taxes_internal(
    document_id: int,
    retentions: list[DocumentTaxCreateRequest],
    db: Session = Depends(_get_tenant_db_internal),
):
    """RF-08: guarda las retenciones que la IA determinó al procesar el documento.

    Sin esta persistencia la determinación automática sería inútil: no hay nadie
    escuchando la respuesta cuando se dispara desde el procesamiento del XML, así que la
    propuesta debe quedar en el documento para que el contador la vea en la sección de
    RF-02 y la confirme o la ajuste.

    Todas las filas quedan marcadas con `source="llm"` sin importar lo que envíe el
    cliente: es lo que permite a la interfaz distinguirlas del trabajo manual del contador
    y advertirle antes de regenerarlas.

    Idempotente por `tax_id`: reprocesar un XML no duplica retenciones ni pisa las que el
    contador ya registró a mano. Tampoco agrega una segunda retención del mismo tipo
    (RETEICA/RETEIVA/RETEFUENTE) que ya esté presente por otra vía — la misma regla que
    aplica la ruta pública de creación manual (`document_taxes.py`).
    """
    repo = DocumentRepository(db)
    if repo.get_by_id(document_id) is None:
        raise HTTPException(status_code=404, detail=f"Document {document_id} not found")

    tax_repo = DocumentTaxRepository(db)
    # Combinado: las sugerencias de la IA traen un `tax_id` de `integration_retentions` casi
    # siempre, pero el catálogo real del cliente también registra impuestos del documento
    # (Impoconsumo) en `document_taxes`, así que la validación no puede asumir una sola tabla.
    integration_tax_repo = TaxOrRetentionRepository(db)
    existing_taxes = tax_repo.list_by_document(document_id)
    existing_ids = {row.tax_id for row in existing_taxes}
    existing_types = {
        str(t.type or "").strip().lower()
        for t in (integration_tax_repo.get_by_id(row.tax_id) for row in existing_taxes)
        if t is not None
    }

    # Solo se persisten retenciones cuyo tax_id exista y esté activo en el catálogo. El LLM
    # ya elige de las candidatas, pero esta ruta corre sin nadie escuchando, así que se
    # descarta —no se aborta— cualquier propuesta con un tax_id inválido, del mismo modo que
    # la asignación de cuentas del modelo (RF-04). Evita retenciones huérfanas en la base.
    catalog = {t.id: t for t in integration_tax_repo.get_active()}

    created = 0
    skipped = 0
    for retention in retentions:
        if retention.tax_id in existing_ids:
            skipped += 1
            continue
        tax = catalog.get(retention.tax_id)
        if catalog and tax is None:
            logger.warning(
                "Retención sugerida descartada (doc=%s, tax_id=%s): no está activa en el catálogo.",
                document_id,
                retention.tax_id,
            )
            skipped += 1
            continue
        tipo = str(tax.type or "").strip().lower() if tax is not None else ""
        if tipo and tipo in existing_types:
            logger.warning(
                "Retención sugerida descartada (doc=%s, tax_id=%s): ya hay una retención "
                "de tipo '%s' en el documento.",
                document_id,
                retention.tax_id,
                tipo,
            )
            skipped += 1
            continue
        tax_repo.create(
            document_id,
            retention.tax_id,
            retention.taxable_base,
            retention.percentage,
            source="llm",
        )
        # El alta se refleja de inmediato para que un mismo lote con `tax_id` repetido, o con
        # dos tax_id distintos del mismo tipo, no inserte más de una fila.
        existing_ids.add(retention.tax_id)
        if tipo:
            existing_types.add(tipo)
        created += 1

    return DocumentTaxSuggestionResponse(created=created, skipped=skipped)


@router.post(
    "/internal/catalog/cost-centers/projections",
    include_in_schema=False,
    dependencies=[Depends(_verify_internal_secret)],
    status_code=201,
)
def project_cost_centers_internal(
    items: list[CostCenterProjectionItem],
    db: Session = Depends(_get_tenant_db_internal),
):
    """Proyecta sobre `cost_centers` el catálogo que integration-config-service sincronizó
    desde SIIGO (`integration_cost_centers`).

    `cost_centers` es propiedad de este servicio y es la tabla que alimenta
    `GET /api/v1/catalog/cost-centers`, por lo que la escritura se centraliza aquí en vez
    de que otro servicio toque la tabla directamente.
    """
    repo = CostCenterRepository(db)
    created, updated = repo.upsert_many([i.model_dump() for i in items])
    return CostCenterProjectionResponse(
        created=created, updated=updated, total=len(repo.get_active())
    )


@router.post(
    "/internal/catalog/puc-accounts/projections",
    include_in_schema=False,
    dependencies=[Depends(_verify_internal_secret)],
    status_code=201,
)
def project_puc_accounts_internal(
    items: list[PucAccountProjectionItem],
    db: Session = Depends(_get_tenant_db_internal),
):
    """Proyecta sobre `puc_accounts` el plan de cuentas que integration-config-service
    importó desde Excel (`integration_chart_accounts`).

    `puc_accounts` es propiedad de este servicio: alimenta `GET /api/v1/catalog/puc-accounts`,
    que consumen tanto el selector de cuenta del frontend como el llm-service para acotar y
    validar las sugerencias del modelo.
    """
    repo = PucRepository(db)
    created, updated = repo.upsert_many([i.model_dump() for i in items])
    return PucAccountProjectionResponse(
        created=created, updated=updated, total=len(repo.get_active())
    )
