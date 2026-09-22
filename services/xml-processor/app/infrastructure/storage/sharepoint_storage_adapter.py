import asyncio
from typing import Optional
from urllib.parse import quote

import httpx
import msal

from app.domain.ports.storage import DocumentStoragePort

_GRAPH_BASE_URL = "https://graph.microsoft.com/v1.0"
_GRAPH_SCOPE = ["https://graph.microsoft.com/.default"]


class SharePointStorageAdapter(DocumentStoragePort):
    """Sube archivos a una biblioteca de SharePoint via Microsoft Graph API,
    usando autenticacion app-only (client credentials)."""

    def __init__(
        self,
        azure_tenant_id: str,
        client_id: str,
        client_secret: str,
        site_id: str,
        drive_id: str,
        folder_path: str = "",
    ):
        self._site_id = site_id
        self._drive_id = drive_id
        self._folder_path = folder_path.strip("/")
        self._azure_tenant_id = azure_tenant_id
        self._client_id = client_id
        self._client_secret = client_secret
        self._app: Optional[msal.ConfidentialClientApplication] = None

    def _get_app(self) -> msal.ConfidentialClientApplication:
        # Construccion perezosa: el constructor de MSAL hace descubrimiento OIDC por red,
        # que no debe ocurrir hasta que realmente se necesite subir un archivo.
        if self._app is None:
            self._app = msal.ConfidentialClientApplication(
                client_id=self._client_id,
                client_credential=self._client_secret,
                authority=f"https://login.microsoftonline.com/{self._azure_tenant_id}",
            )
        return self._app

    def _acquire_token(self) -> str:
        result = self._get_app().acquire_token_for_client(scopes=_GRAPH_SCOPE)
        if "access_token" not in result:
            raise RuntimeError(
                f"No se pudo obtener token de Microsoft Graph: {result.get('error_description', result)}"
            )
        return result["access_token"]

    async def save(self, content: bytes, filename: str) -> str:
        token = await asyncio.to_thread(self._acquire_token)
        path = f"{self._folder_path}/{filename}" if self._folder_path else filename
        encoded_path = quote(path)
        url = (
            f"{_GRAPH_BASE_URL}/sites/{self._site_id}/drives/{self._drive_id}"
            f"/root:/{encoded_path}:/content"
        )
        async with httpx.AsyncClient(timeout=30.0) as client:
            response = await client.put(
                url,
                content=content,
                headers={
                    "Authorization": f"Bearer {token}",
                    "Content-Type": "application/octet-stream",
                },
            )
        response.raise_for_status()
        return response.json().get("webUrl", url)
