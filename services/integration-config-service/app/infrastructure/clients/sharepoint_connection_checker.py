import uuid
from dataclasses import dataclass
from urllib.parse import quote

import httpx

_GRAPH_BASE_URL = "https://graph.microsoft.com/v1.0"
_TOKEN_URL = "https://login.microsoftonline.com/{tenant}/oauth2/v2.0/token"
_TIMEOUT_SECONDS = 10

_TOKEN_ERROR_MESSAGES = {
    "invalid_client": "El client secret es incorrecto o expiro.",
    "unauthorized_client": "La aplicacion no esta autorizada en el tenant de Azure.",
    "invalid_request": "El Tenant ID o el Client ID no son validos.",
}


@dataclass(frozen=True)
class SharePointConnectionResult:
    ok: bool
    message: str


class SharePointConnectionChecker:
    """Comprueba que unas credenciales de SharePoint permiten escribir en la biblioteca.

    Pide un token app-only a Entra ID y luego sube y borra un archivo de prueba, la misma
    operacion (`PUT .../root:/<ruta>:/content`) que usa xml-processor al publicar. Nunca lanza.
    """

    def check(
        self,
        *,
        azure_tenant_id: str,
        client_id: str,
        client_secret: str,
        site_id: str,
        drive_id: str,
        folder_path: str = "",
    ) -> SharePointConnectionResult:
        try:
            with httpx.Client(timeout=_TIMEOUT_SECONDS) as client:
                token_response = client.post(
                    _TOKEN_URL.format(tenant=quote(azure_tenant_id, safe="")),
                    data={
                        "grant_type": "client_credentials",
                        "client_id": client_id,
                        "client_secret": client_secret,
                        "scope": "https://graph.microsoft.com/.default",
                    },
                )
                if token_response.status_code != 200:
                    error = token_response.json().get("error", "") if token_response.content else ""
                    return SharePointConnectionResult(
                        ok=False,
                        message=_TOKEN_ERROR_MESSAGES.get(
                            error,
                            f"Microsoft rechazo las credenciales ({error or token_response.status_code}).",
                        ),
                    )
                headers = {"Authorization": f"Bearer {token_response.json()['access_token']}"}

                folder = folder_path.strip("/")
                probe = f".abacus-connection-test-{uuid.uuid4().hex}"
                path = quote(f"{folder}/{probe}" if folder else probe)
                drive_url = f"{_GRAPH_BASE_URL}/sites/{site_id}/drives/{drive_id}"
                upload = client.put(
                    f"{drive_url}/root:/{path}:/content",
                    content=b"",
                    headers={**headers, "Content-Type": "application/octet-stream"},
                )
                if upload.status_code in (401, 403):
                    return SharePointConnectionResult(
                        ok=False,
                        message=(
                            "La aplicacion no tiene permiso de escritura en el sitio "
                            "(revise Sites.ReadWrite.All o Sites.Selected)."
                        ),
                    )
                if upload.status_code == 404:
                    return SharePointConnectionResult(
                        ok=False,
                        message="No se encontro el sitio o la biblioteca. Revise Site ID y Drive ID.",
                    )
                if upload.status_code == 400:
                    return SharePointConnectionResult(
                        ok=False, message="El Site ID o el Drive ID tienen un formato invalido."
                    )
                if upload.status_code >= 400:
                    return SharePointConnectionResult(
                        ok=False,
                        message=f"SharePoint rechazo la subida de prueba (HTTP {upload.status_code}).",
                    )
                item_id = upload.json().get("id")
                if item_id:
                    client.delete(f"{drive_url}/items/{item_id}", headers=headers)
        except httpx.HTTPError as exc:
            return SharePointConnectionResult(
                ok=False, message=f"No se pudo conectar con Microsoft: {exc.__class__.__name__}."
            )
        except (ValueError, KeyError):
            return SharePointConnectionResult(ok=False, message="Respuesta inesperada de Microsoft.")
        return SharePointConnectionResult(
            ok=True, message="Conexion exitosa: la biblioteca es accesible y escribible."
        )
