from typing import Optional

from app.application.dto.integration import CredentialUpsertRequest
from app.domain.exceptions.base import ValidationException
from app.infrastructure.clients.siigo_client import token_expiration_from_response
from app.infrastructure.clients.siigo_connection_checker import SiigoConnectionChecker
from app.infrastructure.persistence.models.integration import IntegrationCredential
from app.infrastructure.persistence.repositories.integration_repository import (
    IntegrationCredentialRepository,
)

_SIIGO_PROVIDER = "siigo"


class ManageCredentialsUseCase:
    def __init__(
        self,
        repository: IntegrationCredentialRepository,
        siigo_connection_checker: SiigoConnectionChecker | None = None,
    ):
        self.repository = repository
        self.siigo_connection_checker = siigo_connection_checker or SiigoConnectionChecker()

    def upsert(self, request: CredentialUpsertRequest):
        """Valida (si es SIIGO) y guarda la credencial.

        Devuelve `(credencial, resultado)`; `resultado` es None para proveedores sin
        validacion. Si SIIGO no autentica, lanza `ValidationException` y no guarda nada.
        """
        provider = request.provider.strip().lower()
        account_key = request.account_key.strip()
        if not provider:
            raise ValidationException("provider is required")
        if not account_key:
            raise ValidationException("account_key is required")

        connection = None
        if provider == _SIIGO_PROVIDER:
            # Credencial transitoria (no se agrega a la sesion): se prueba antes de persistir.
            candidate = IntegrationCredential(
                provider=provider,
                account_key=account_key,
                username=request.username,
                access_key=request.access_key,
                base_url=str(request.base_url),
                partner_id=request.partner_id,
                auth_scheme=request.auth_scheme,
            )
            connection = self.siigo_connection_checker.check(candidate)
            if not connection.ok:
                raise ValidationException(connection.message)

        credential = self.repository.upsert(
            provider=provider,
            account_key=account_key,
            username=request.username,
            access_key=request.access_key,
            base_url=str(request.base_url),
            partner_id=request.partner_id,
            auth_scheme=request.auth_scheme,
            extra_config=request.extra_config,
        )
        if connection is None:
            return credential, None

        # El token recien emitido evita una autenticacion extra en la primera sincronizacion.
        self.repository.save_token(
            provider=provider,
            account_key=account_key,
            access_token=connection.auth_response["access_token"],
            token_type=connection.auth_response.get("token_type", "Bearer"),
            expires_at=token_expiration_from_response(connection.auth_response),
        )
        return credential, connection

    def list(self, provider: Optional[str] = None):
        return self.repository.list(provider=provider.strip().lower() if provider else None)
