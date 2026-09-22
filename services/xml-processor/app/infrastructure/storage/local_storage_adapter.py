from pathlib import Path

from app.domain.ports.storage import DocumentStoragePort


class LocalStorageAdapter(DocumentStoragePort):
    """Guarda el archivo en un subdirectorio local (comportamiento por defecto)."""

    def __init__(self, target_dir: Path):
        self._target_dir = target_dir
        self._target_dir.mkdir(parents=True, exist_ok=True)

    async def save(self, content: bytes, filename: str) -> str:
        dest = self._target_dir / filename
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(content)
        return str(dest)
