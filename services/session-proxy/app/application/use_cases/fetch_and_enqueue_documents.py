import logging
import os
import re
import uuid
from datetime import datetime, timezone
from urllib.parse import unquote

from app.application.dto.documents import DocumentsRangeRequest, EnqueueDownloadsResponse
from app.domain.exceptions.base import (
    FiscalNitNotConfiguredException,
    NoDocumentsFoundException,
    RkNitMismatchException,
)
from app.domain.ports.queue import JobQueuePort
from app.domain.ports.services import ExternalClientPort, FiscalProfilePort
from app.infrastructure.queue.batch_store import RedisBatchStore
from app.infrastructure.queue.job_progress_store import JobProgressStore

logger = logging.getLogger(__name__)

DOWNLOAD_BATCH_FUNCTION = "download_batch"


class FetchAndEnqueueDocumentsUseCase:
    DOCUMENTS_PATH = "/Document/GetDocumentsPageToken"

    def __init__(
        self,
        external_client: ExternalClientPort,
        job_queue: JobQueuePort,
        base_url: str,
        login_url: str,
        batch_store: RedisBatchStore,
        job_progress_store: JobProgressStore,
        fiscal_profile: FiscalProfilePort,
    ):
        self._fiscal_profile = fiscal_profile
        self._client = external_client
        self._queue = job_queue
        self._base_url = base_url.rstrip("/")
        self._login_url = login_url
        self._batch_store = batch_store
        self._progress = job_progress_store

    @staticmethod
    def _digits(value: str) -> str:
        return re.sub(r"\D", "", unquote(value or ""))

    async def _validate_rk_matches_fiscal_nit(self, request: DocumentsRangeRequest, tenant_slug: str):
        """El `rk` del enlace DIAN es el NIT de la empresa; debe ser el del perfil fiscal.

        Sin esta comprobación un enlace de otra empresa descargaría sus documentos hacia la
        base de este tenant. Se hace antes de tocar la DIAN. Se comparan solo los dígitos para
        no depender de cómo venga escrito. Usa el mismo `rk` efectivo que luego se envía a la
        DIAN (el del enlace o, si viene vacío, `EXTERNAL_FIXED_RK`).
        """
        registered = self._digits(await self._fiscal_profile.get_nit(tenant_slug) or "")
        if not registered:
            raise FiscalNitNotConfiguredException()
        link_rk = self._digits(request.rk or os.getenv("EXTERNAL_FIXED_RK", ""))
        if link_rk != registered:
            logger.warning(
                "rk del enlace no coincide con el NIT del perfil fiscal (tenant=%s)", tenant_slug
            )
            raise RkNitMismatchException()

    async def execute(
        self, request: DocumentsRangeRequest, tenant_slug: str = ""
    ) -> EnqueueDownloadsResponse:
        await self._validate_rk_matches_fiscal_nit(request, tenant_slug)
        batch_id = str(uuid.uuid4())
        started_at = datetime.now(timezone.utc)

        body = request.model_dump(exclude={"token"})
        documents_url = f"{self._base_url}{self.DOCUMENTS_PATH}"

        result = await self._client.login_and_request(
            login_url=self._login_url,
            credentials={"token": request.token, "pk": request.pk, "rk": request.rk},
            method="POST",
            url=documents_url,
            body=body,
        )

        raw_docs = result.get("body", [])
        if isinstance(raw_docs, dict):
            raw_docs = raw_docs.get("data", [])
        if not isinstance(raw_docs, list):
            raw_docs = []

        logger.info(
            "Documentos encontrados en rango %s→%s: %d",
            request.StartDate,
            request.EndDate,
            len(raw_docs),
        )
        if not raw_docs:
            raise NoDocumentsFoundException(request.StartDate, request.EndDate)

        logger.info("Claves del primer documento: %s", list(raw_docs[0].keys()))
        logger.info("Primer documento (crudo): %s", raw_docs[0])

        # El progreso se lleva por trackId (clave = track_id). El endpoint de estado
        # itera estos "job_ids" lógicos; el job ARQ real que descarga el lote es uno solo.
        track_ids: list[str] = []
        job_track_map: dict[str, str] = {}
        for doc in raw_docs:
            track_id = doc.get("Id") or doc.get("id")
            if not track_id:
                continue
            doc_type_id = doc.get("DocumentTypeId") or doc.get("documentTypeId")
            if str(doc_type_id) == "96":
                logger.info("Documento ignorado (DocumentTypeId=96) — trackId: %s", track_id)
                continue
            track_id = str(track_id)
            track_ids.append(track_id)
            job_track_map[track_id] = track_id
            await self._progress.init(track_id, track_id)

        if track_ids:
            arq_job_id = await self._queue.enqueue(
                DOWNLOAD_BATCH_FUNCTION,
                batch_id=batch_id,
                track_ids=track_ids,
                token=request.token,
                pk=request.pk,
                rk=request.rk,
                tenant_slug=tenant_slug,
            )
            logger.info(
                "Batch %s encolado (job ARQ %s) con %d documentos",
                batch_id,
                arq_job_id,
                len(track_ids),
            )
        else:
            logger.info("Batch %s sin documentos descargables", batch_id)

        await self._batch_store.save(batch_id, track_ids, started_at, job_track_map)

        return EnqueueDownloadsResponse(
            batch_id=batch_id,
            enqueued=len(track_ids),
            job_ids=track_ids,
            StartDate=request.StartDate,
            EndDate=request.EndDate,
            started_at=started_at.isoformat(),
        )
