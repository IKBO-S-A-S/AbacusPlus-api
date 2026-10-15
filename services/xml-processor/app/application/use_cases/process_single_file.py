import asyncio
import logging
import time
from pathlib import Path
from typing import Optional

from app.application.dto.batch import EnqueueBatchResponse

logger = logging.getLogger(__name__)


class ProcessSingleFileUseCase:
    def __init__(self, downloads_dir: str, queue: asyncio.Queue):
        self._dir = Path(downloads_dir)
        self._queue = queue

    async def execute(
        self, filename: str, job_id: str, tenant_slug: str = ""
    ) -> Optional[EnqueueBatchResponse]:
        file_path = self._dir / filename
        if not file_path.is_file():
            logger.warning("Archivo no encontrado: %s", file_path)
            return None

        # El 4.º elemento (instante de encolado) solo sirve a las métricas: permite medir cuánto
        # espera cada ZIP detrás de los demás en la cola de un solo consumidor.
        await self._queue.put((file_path, job_id, tenant_slug, time.monotonic()))
        logger.info(
            "ZIP encolado para procesamiento: %s (job_id=%s, tenant=%s)",
            filename,
            job_id,
            tenant_slug,
        )

        return EnqueueBatchResponse(queued=1, files=[filename])
