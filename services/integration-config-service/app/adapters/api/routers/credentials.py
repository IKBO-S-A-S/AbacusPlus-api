from typing import Optional

from fastapi import APIRouter, Depends, status

from app.application.dto.connection import ConnectionCheckResponse
from app.application.dto.integration import CredentialResponse, CredentialUpsertRequest
from app.application.use_cases.manage_credentials import ManageCredentialsUseCase
from app.dependencies import get_credentials_use_case
from app.infrastructure.config.auth_dependency import require_write

router = APIRouter()


@router.put(
    "/integrations/credentials",
    dependencies=[Depends(require_write)],
    response_model=CredentialResponse,
    status_code=status.HTTP_200_OK,
    summary="Registrar credenciales de una integracion",
    description=(
        "Crea o actualiza credenciales en la tabla generica `integration_credentials`.\n\n"
        "Este endpoint es agnostico al proveedor: puede almacenar SIIGO, Odoo, Alegra "
        "u otras plataformas futuras usando `provider`, `account_key`, `auth_scheme` "
        "y `extra_config` para parametros propios de cada adaptador.\n\n"
        "Para `provider=siigo`, antes de guardar se autentica contra SIIGO. Si no se obtiene "
        "token, responde 400 con el motivo y **no** guarda ni modifica nada. Si autentica, "
        "guarda y el resultado viaja en `connection`. Otros proveedores no se validan "
        "(`connection` queda en `null`)."
    ),
    response_description=(
        "Credencial registrada sin exponer secretos y, para SIIGO, el resultado de la "
        "validacion de conexion en `connection`."
    ),
    responses={
        400: {
            "description": (
                "Payload invalido, proveedor vacio o SIIGO no acepto las credenciales "
                "(no se guarda)."
            )
        }
    },
)
def upsert_credentials(
    request: CredentialUpsertRequest,
    use_case: ManageCredentialsUseCase = Depends(get_credentials_use_case),
) -> CredentialResponse:
    credential, connection = use_case.upsert(request)
    response = CredentialResponse.model_validate(credential)
    if connection is not None:
        response.connection = ConnectionCheckResponse(ok=connection.ok, message=connection.message)
    return response


@router.get(
    "/integrations/credentials",
    response_model=list[CredentialResponse],
    status_code=status.HTTP_200_OK,
    summary="Listar credenciales de integraciones",
    description=(
        "Lista las credenciales configuradas para uno o todos los proveedores. "
        "La respuesta omite secretos como `access_key` y `access_token`."
    ),
    response_description="Listado de credenciales no sensibles.",
)
def list_credentials(
    provider: Optional[str] = None,
    use_case: ManageCredentialsUseCase = Depends(get_credentials_use_case),
) -> list[CredentialResponse]:
    return use_case.list(provider=provider)
