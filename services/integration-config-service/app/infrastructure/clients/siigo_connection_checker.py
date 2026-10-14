from dataclasses import dataclass
from typing import Any, Optional

from app.domain.exceptions.base import ExternalAuthException, ExternalServiceException
from app.infrastructure.clients.siigo_client import SiigoApiClient
from app.infrastructure.persistence.models.integration import IntegrationCredential


@dataclass(frozen=True)
class SiigoConnectionResult:
    ok: bool
    message: str
    #: Respuesta de autenticacion de SIIGO cuando fue exitosa, para reutilizar el token.
    auth_response: Optional[dict[str, Any]] = None


class SiigoConnectionChecker:
    """Comprueba que las credenciales guardadas autentican contra SIIGO. Nunca lanza."""

    def check(self, credential: IntegrationCredential) -> SiigoConnectionResult:
        try:
            auth_response = SiigoApiClient(credential, timeout=10.0).authenticate()
        except ExternalAuthException:
            return SiigoConnectionResult(
                ok=False,
                message="SIIGO rechazo las credenciales: revise el usuario, el access key y el Partner-Id.",
            )
        except ExternalServiceException:
            return SiigoConnectionResult(
                ok=False, message="No se pudo conectar con SIIGO. Revise la URL base y su red."
            )
        except (ValueError, KeyError):
            return SiigoConnectionResult(ok=False, message="Respuesta inesperada de SIIGO.")
        return SiigoConnectionResult(
            ok=True, message="Conexion exitosa: SIIGO acepto las credenciales.", auth_response=auth_response
        )
