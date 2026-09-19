from abc import ABC, abstractmethod


class DocumentStoragePort(ABC):
    """Guarda un artefacto (PDF o XML) generado al procesar un documento DIAN."""

    @abstractmethod
    async def save(self, content: bytes, filename: str) -> str:
        """Persiste `content` bajo `filename` y retorna la ubicacion resultante
        (ruta local, URI s3://, o URL de Microsoft Graph)."""
        ...
