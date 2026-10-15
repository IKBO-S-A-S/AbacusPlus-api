from abc import ABC, abstractmethod
from typing import Any, Optional


class FiscalProfilePort(ABC):
    @abstractmethod
    async def get_nit(self, tenant_slug: str) -> Optional[str]:
        """NIT registrado en el perfil fiscal del tenant, o None si aún no lo tiene."""
        ...


class ExternalClientPort(ABC):
    @abstractmethod
    async def login(self, login_url: str, credentials: dict[str, Any]) -> dict[str, str]:
        """Autentica con el portal externo. Retorna cookies capturadas {nombre: valor}."""
        ...

    @abstractmethod
    async def request(
        self,
        method: str,
        url: str,
        cookies: dict[str, str],
        body: Optional[dict[str, Any]] = None,
        params: Optional[dict[str, Any]] = None,
    ) -> dict[str, Any]:
        """Reenvía una petición usando las cookies de sesión. Retorna {status_code, body, headers}."""
        ...

    @abstractmethod
    async def login_and_request(
        self,
        login_url: str,
        credentials: dict[str, Any],
        method: str,
        url: str,
        body: Optional[dict[str, Any]] = None,
        params: Optional[dict[str, Any]] = None,
    ) -> dict[str, Any]:
        """Autentica y hace la petición en un único cliente para preservar las cookies."""
        ...

    @abstractmethod
    async def login_and_download(
        self,
        login_url: str,
        credentials: dict[str, Any],
        download_url: str,
    ) -> bytes:
        """Autentica y descarga contenido binario (ZIP). Retorna bytes del archivo."""
        ...


class ProcessedDocumentsPort(ABC):
    @abstractmethod
    async def existing_track_ids(self, tenant_slug: str, track_ids: list[str]) -> set[str]:
        """trackId (en minúsculas) de los documentos que el tenant ya tiene guardados completos."""
        ...
