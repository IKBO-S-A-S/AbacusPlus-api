import hmac
import os
from typing import Annotated

import jwt as pyjwt
from fastapi import Depends, Header, HTTPException, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from app.infrastructure.config.jwt_validator import decode_token
from app.infrastructure.config.tenant_connection_manager import get_session_for_tenant

_bearer = HTTPBearer()


class TokenData:
    def __init__(self, payload: dict, raw_token: str):
        self.user_id = payload["sub"]
        self.tenant_id = payload["tenant_id"]
        self.tenant_slug = payload["tenant_slug"]
        self.roles: list[str] = payload.get("roles", [])
        self.email: str = payload.get("email", "")
        self.raw_token = raw_token


def get_token_data(
    credentials: HTTPAuthorizationCredentials = Depends(_bearer),
) -> TokenData:
    try:
        payload = decode_token(credentials.credentials)
    except pyjwt.ExpiredSignatureError:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Token expired")
    except pyjwt.PyJWTError:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid token")
    if payload.get("type") != "access":
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Not an access token")
    return TokenData(payload, credentials.credentials)


#: Roles que pueden modificar la imputación de un documento o contabilizarlo.
#:
#: El sistema define tres roles (`tenant_admin`, `operator`, `viewer`) y hasta ahora ninguno
#: se comprobaba: un usuario invitado como «solo lectura» tenía exactamente las mismas
#: facultades que un administrador, incluida la de crear facturas de compra reales en SIIGO.
#:
#: `viewer` queda deliberadamente fuera: es el único rol cuyo nombre promete una limitación,
#: y quien invita a alguien con él da por hecho que no podrá alterar la contabilidad.
ROLES_ESCRITURA = frozenset({"tenant_admin", "operator"})


def require_roles(*permitidos: str):
    """Dependencia que exige al usuario alguno de los roles indicados.

    Se aplica solo a los endpoints que escriben. Las lecturas siguen abiertas a cualquier
    usuario autenticado del cliente, que es lo que `viewer` significa.

    Un token sin ningún rol se rechaza: todos los usuarios reciben uno al crearse —el
    administrador al aprovisionar el cliente, los demás al ser invitados—, así que la
    ausencia de rol no es un caso legítimo, sino un token manipulado o un usuario en un
    estado inconsistente. Denegar es la única respuesta segura.
    """
    permitidos_set = frozenset(permitidos)

    def _verificar(token: Annotated[TokenData, Depends(get_token_data)]) -> TokenData:
        if not permitidos_set.intersection(token.roles):
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail=(
                    "Su usuario no tiene permiso para esta operación. "
                    f"Se requiere uno de estos roles: {', '.join(sorted(permitidos_set))}."
                ),
            )
        return token

    return _verificar


#: Atajo para el caso mayoritario: escribir sobre un documento (RF-01, 02, 03, 05, 06, 07).
require_write = require_roles(*ROLES_ESCRITURA)


def require_internal_secret(x_internal_secret: str = Header(...)) -> None:
    """Autentica llamadas servicio-a-servicio contra `INTERNAL_SECRET`, sin sesión de usuario.

    `require_write` no sirve para el worker de descargas (session-proxy-worker): dispara
    `POST /batch-jobs/file` justo después de bajar un ZIP de la DIAN, sin ningún usuario
    detrás de esa llamada. `require_write` la rechazaba con 401 en cada intento — el ZIP
    quedaba descargado pero nunca se procesaba, y nada lo dejaba visible salvo un warning
    en el log de texto del worker.
    """
    expected = os.environ.get("INTERNAL_SECRET", "")
    if not expected or not hmac.compare_digest(x_internal_secret, expected):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Forbidden")


def get_tenant_db(token: Annotated[TokenData, Depends(get_token_data)]):
    db = get_session_for_tenant(token.tenant_slug)
    try:
        yield db
    finally:
        db.close()
