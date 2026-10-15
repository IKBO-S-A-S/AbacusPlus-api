import hmac
import os

from fastapi import APIRouter, Depends, Header, HTTPException, Query, status
from sqlalchemy import NullPool, create_engine

from app.application.dto.accounting import CodeAssignmentResponse, RetentionSuggestionResponse
from app.application.use_cases.assign_account_codes import AssignAccountCodesUseCase
from app.application.use_cases.suggest_retentions import SuggestRetentionsUseCase
from app.dependencies import (
    get_internal_assign_account_codes_use_case,
    get_internal_suggest_retentions_use_case,
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

    user = os.environ["DATABASE_USER"]
    password = os.environ["DATABASE_PASSWORD"]
    host = os.environ["DATABASE_HOST"]
    port = os.environ.get("DATABASE_PORT", "5432")
    url = f"postgresql+psycopg2://{user}:{password}@{host}:{port}/abacus_t_{tenant_slug}"
    engine = create_engine(url, poolclass=NullPool)
    Base.metadata.create_all(bind=engine, checkfirst=True)
    engine.dispose()
    return {"status": "provisioned", "tenant_slug": tenant_slug}


@router.post(
    "/internal/accounting/code-assignments/{document_id}",
    response_model=CodeAssignmentResponse,
    include_in_schema=False,
    dependencies=[Depends(_verify_internal_secret)],
)
async def assign_account_codes_internal(
    document_id: int,
    overwrite_manual: bool = Query(False),
    use_case: AssignAccountCodesUseCase = Depends(get_internal_assign_account_codes_use_case),
):
    """Asigna cuentas PUC a un documento (servicio-a-servicio).

    Es la misma operación que `POST /accounting/code-assignments/{id}`, para quien no tiene
    JWT de usuario: el xml-processor la dispara desde el worker de descargas de la DIAN.
    Autentica con `X-Internal-Secret` y el tenant sale de `X-Tenant-Slug`. Sin esta ruta la
    llamada respondía 401 y los documentos descargados nunca recibían cuentas.
    """
    try:
        result = await use_case.execute(document_id, overwrite_manual=overwrite_manual)
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc))
    return CodeAssignmentResponse(**result)


@router.post(
    "/internal/accounting/retention-suggestions/{document_id}",
    response_model=RetentionSuggestionResponse,
    include_in_schema=False,
    dependencies=[Depends(_verify_internal_secret)],
)
async def suggest_retentions_internal(
    document_id: int,
    overwrite_manual: bool = Query(False),
    persist: bool = Query(False),
    use_case: SuggestRetentionsUseCase = Depends(get_internal_suggest_retentions_use_case),
):
    """Sugiere retenciones para un documento (servicio-a-servicio). Ver la ruta de cuentas.

    Misma operación que `POST /accounting/retention-suggestions/{id}`; con `persist=true` la
    propuesta queda guardada con origen `llm`, que es lo que necesita el procesamiento
    automático, donde no hay interfaz esperando la respuesta.
    """
    try:
        result = await use_case.execute(
            document_id, overwrite_manual=overwrite_manual, persist=persist
        )
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc))
    return RetentionSuggestionResponse(**result)
