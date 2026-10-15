import hmac
import os

from fastapi import APIRouter, Depends, Header, HTTPException
from sqlalchemy import NullPool, create_engine

from app.application.dto.chunk import (
    IndexChunkRequest,
    IndexChunkResponse,
    InternalIndexChunkRequest,
    InternalRevokeChunkRequest,
    RevokeChunkResponse,
    SearchRequest,
    SearchResponse,
)

router = APIRouter()


def _verify_internal_secret(x_internal_secret: str = Header(...)):
    expected = os.environ.get("INTERNAL_SECRET", "")
    if not expected or not hmac.compare_digest(x_internal_secret, expected):
        raise HTTPException(status_code=403, detail="Forbidden")


@router.post(
    "/internal/provision-tenant",
    include_in_schema=False,
    dependencies=[Depends(_verify_internal_secret)],
)
def provision_tenant(tenant_slug: str):
    """Create all tables for this service in the tenant DB. Called by auth-service during tenant registration."""
    from app.infrastructure.config.database import Base
    from app.infrastructure.persistence.tenant_migrations import run_tenant_migrations

    user = os.environ["DATABASE_USER"]
    password = os.environ["DATABASE_PASSWORD"]
    host = os.environ["DATABASE_HOST"]
    port = os.environ.get("DATABASE_PORT", "5432")
    url = f"postgresql+psycopg2://{user}:{password}@{host}:{port}/abacus_t_{tenant_slug}"
    engine = create_engine(url, poolclass=NullPool)
    Base.metadata.create_all(bind=engine, checkfirst=True)
    # `create_all` no altera una tabla que ya existe: las columnas añadidas después del
    # aprovisionamiento inicial solo llegan por aquí.
    run_tenant_migrations(engine, tenant_slug)
    engine.dispose()
    return {
        "status": "provisioned",
        "tenant_slug": tenant_slug,
        "service": __import__("os").environ.get("SERVICE_NAME", "unknown"),
    }


@router.post(
    "/internal/chunks",
    response_model=IndexChunkResponse,
    status_code=201,
    include_in_schema=False,
    dependencies=[Depends(_verify_internal_secret)],
)
async def index_chunk_internal(request: InternalIndexChunkRequest):
    """Indexa un chunk en la BD del tenant indicado (servicio-a-servicio).

    Es la vía que usa el pipeline de contabilización, que puede correr en segundo plano y no
    dispone de un JWT de usuario para que `get_tenant_db` resuelva el tenant. El
    `tenant_slug` llega explícito en el body y la sesión se abre contra `abacus_t_{slug}`, de
    modo que los embeddings quedan aislados por tenant igual que en `POST /chunks`.

    RF-08: es también la única vía por la que puede entrar conocimiento validado
    (`is_validated=True`), porque solo el xml-processor sabe si el documento quedó
    CONTABILIZADO y con qué identificador de SIIGO.
    """
    from app.application.use_cases.index_chunk import IndexChunkUseCase
    from app.dependencies import get_embedding_service
    from app.infrastructure.config.tenant_connection_manager import get_session_for_tenant
    from app.infrastructure.persistence.repositories.chunk_repository import ChunkRepository

    db = get_session_for_tenant(request.tenant_slug)
    try:
        repo = ChunkRepository(db)
        # Upsert por documento: si el chunk ya existía (p. ej. una causación contabilizada
        # que se reconcilió después), se reemplaza en vez de duplicarse. Sin esto convivirían
        # dos versiones del mismo documento y la búsqueda devolvería datos contradictorios.
        if request.source_id is not None:
            repo.delete_by_source(request.source_type, request.source_id)
        use_case = IndexChunkUseCase(
            chunk_repo=repo,
            embedding_service=get_embedding_service(),
        )
        return await use_case.execute(
            IndexChunkRequest(
                source_type=request.source_type,
                source_id=request.source_id,
                content=request.content,
                is_validated=request.is_validated,
                siigo_id=request.siigo_id,
                metadata=request.metadata,
            )
        )
    finally:
        db.close()


@router.post(
    "/internal/chunks/revoke",
    response_model=RevokeChunkResponse,
    include_in_schema=False,
    dependencies=[Depends(_verify_internal_secret)],
)
async def revoke_chunk_internal(request: InternalRevokeChunkRequest):
    """RF-08: retira del RAG el conocimiento de un documento (servicio-a-servicio).

    Lo llama el xml-processor cuando un documento deja de estar contabilizado —una reversión
    o un ajuste válido—. Una causación que ya no refleja ningún asiento vigente no puede
    seguir sirviendo de precedente: si se mantuviera, el error que motivó el ajuste se
    propagaría a todos los documentos parecidos que vengan después.

    Es idempotente: revocar algo que ya no está devuelve `deleted = 0` sin error.
    """
    from app.infrastructure.config.tenant_connection_manager import get_session_for_tenant
    from app.infrastructure.persistence.repositories.chunk_repository import ChunkRepository

    db = get_session_for_tenant(request.tenant_slug)
    try:
        deleted = ChunkRepository(db).delete_by_source(request.source_type, request.source_id)
        return RevokeChunkResponse(
            source_type=request.source_type,
            source_id=request.source_id,
            deleted=deleted,
        )
    finally:
        db.close()


@router.post(
    "/internal/chunks/search",
    response_model=SearchResponse,
    include_in_schema=False,
    dependencies=[Depends(_verify_internal_secret)],
)
async def search_chunks_internal(
    request: SearchRequest,
    x_tenant_slug: str = Header(...),
):
    """Búsqueda semántica en la BD del tenant indicado (servicio-a-servicio).

    Es la vía del llm-service cuando asigna cuentas o sugiere retenciones en segundo plano
    (documentos recién descargados de la DIAN), donde no hay un JWT de usuario del que
    `get_tenant_db` pueda sacar el tenant. Misma lógica y mismo cuerpo que
    `POST /chunks/search`; el tenant llega en `X-Tenant-Slug`, de modo que la búsqueda queda
    aislada por tenant igual que con token. Sin esta ruta, la búsqueda respondía 401 y el
    modelo decidía sin precedentes.
    """
    from app.application.use_cases.search_chunks import SearchChunksUseCase
    from app.dependencies import get_embedding_service
    from app.infrastructure.config.tenant_connection_manager import get_session_for_tenant
    from app.infrastructure.persistence.repositories.chunk_repository import ChunkRepository

    db = get_session_for_tenant(x_tenant_slug)
    try:
        use_case = SearchChunksUseCase(
            chunk_repo=ChunkRepository(db),
            embedding_service=get_embedding_service(),
        )
        return await use_case.execute(request)
    finally:
        db.close()
