from app.application.dto.fiscal_profile import (
    FiscalProfileResponse,
    FiscalProfileUpsertRequest,
)
from app.infrastructure.persistence.repositories.tenant_fiscal_profile_repository import (
    TenantFiscalProfileRepository,
)

# Perfil por defecto cuando el tenant aún no lo ha configurado: todo en falso / ordinario.
# Es deliberadamente conservador: sin confirmar que la empresa es agente de retención, no se
# asume que retiene.
_DEFAULT = FiscalProfileResponse(
    nit=None,
    razon_social=None,
    agente_retencion_renta=False,
    agente_retencion_ica=False,
    agente_retencion_iva=False,
    autorretenedor_renta=False,
    gran_contribuyente=False,
    responsable_iva=False,
    regimen="ordinario",
    notas=None,
)


class ManageFiscalProfileUseCase:
    def __init__(self, repo: TenantFiscalProfileRepository):
        self._repo = repo

    def get(self) -> FiscalProfileResponse:
        """Perfil fiscal del tenant. Si no existe, devuelve el default conservador."""
        profile = self._repo.get()
        if profile is None:
            return _DEFAULT
        return FiscalProfileResponse.model_validate(profile)

    def upsert(self, request: FiscalProfileUpsertRequest) -> FiscalProfileResponse:
        saved = self._repo.upsert(request.model_dump())
        return FiscalProfileResponse.model_validate(saved)
