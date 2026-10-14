from fastapi import APIRouter, Depends, status

from app.application.dto.fiscal_profile import (
    FiscalProfileResponse,
    FiscalProfileUpsertRequest,
)
from app.application.use_cases.manage_fiscal_profile import ManageFiscalProfileUseCase
from app.dependencies import get_fiscal_profile_use_case
from app.infrastructure.config.auth_dependency import require_write

router = APIRouter()


@router.get(
    "/integrations/fiscal-profile",
    response_model=FiscalProfileResponse,
    status_code=status.HTTP_200_OK,
    summary="Perfil fiscal de la empresa (tenant)",
    description=(
        "Retorna el perfil fiscal propio de la empresa (el COMPRADOR en las facturas de "
        "compra): si es agente de retención de renta/ICA/IVA, autorretenedor, gran "
        "contribuyente, responsable de IVA y su régimen.\n\n"
        "Los **municipios** donde se retiene ICA no están aquí: son los de la tabla de "
        "tarifas de ReteICA (`GET /api/v1/catalog/retention-ica-rates`), la única que lleva "
        "además la tarifa. Este perfil solo dice **si** la empresa retiene ICA; esa tabla "
        "dice **dónde** y **cuánto**.\n\n"
        "Es AUTORITATIVO sobre lo que trae el XML de la factura: la decisión de retenciones "
        "usa este perfil para saber si la empresa retiene. Si nunca se ha configurado, "
        "devuelve un perfil por defecto conservador (todo en falso, régimen ordinario)."
    ),
    response_description="Perfil fiscal vigente del tenant.",
)
def get_fiscal_profile(
    use_case: ManageFiscalProfileUseCase = Depends(get_fiscal_profile_use_case),
) -> FiscalProfileResponse:
    return use_case.get()


@router.put(
    "/integrations/fiscal-profile",
    dependencies=[Depends(require_write)],
    response_model=FiscalProfileResponse,
    status_code=status.HTTP_200_OK,
    summary="Configurar el perfil fiscal de la empresa",
    description=(
        "Crea o actualiza (upsert) el perfil fiscal del tenant. Es un singleton: siempre hay "
        "un único perfil por empresa. Lo diligencia el usuario con la información que confirma "
        "el contador, y a partir de ahí manda sobre el `TaxLevelCode` del XML.\n\n"
        "Reglas de negocio que habilita: solo si `agente_retencion_renta` (o el equivalente de "
        "ICA/IVA) es verdadero se practican esas retenciones; `regimen` controla el trato del "
        "Régimen Simple."
    ),
    response_description="Perfil fiscal actualizado.",
    responses={
        422: {
            "description": (
                "Régimen inválido, NIT con caracteres no numéricos (o más de 20 dígitos) "
                "u otros datos fuera de rango."
            )
        }
    },
)
def upsert_fiscal_profile(
    request: FiscalProfileUpsertRequest,
    use_case: ManageFiscalProfileUseCase = Depends(get_fiscal_profile_use_case),
) -> FiscalProfileResponse:
    return use_case.upsert(request)
