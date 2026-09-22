from datetime import datetime, timezone
from typing import Optional

from app.application.dto.documents import (
    BatchStatusResponse,
    BatchStepSummary,
    JobProgressDetail,
    JobSteps,
    StepAccounting,
    StepDownloaded,
    StepError,
    StepSummary,
    StepXmlProcessed,
)
from app.infrastructure.queue.batch_store import RedisBatchStore
from app.infrastructure.queue.job_progress_store import JobProgressStore


def _parse_bool(val: str) -> bool:
    return val == "1"


def _or_none(val: str) -> Optional[str]:
    return val if val else None


def _current_step(data: dict) -> str:
    if not _parse_bool(data.get("downloaded_done", "0")):
        return "downloaded"
    if not _parse_bool(data.get("xml_done", "0")):
        return "xml_processed"
    if not _parse_bool(data.get("accounting_done", "0")):
        if data.get("xml_status") == "error":
            return "error"
        return "accounting"
    return "done"


def _build_job_detail(job_id: str, data: dict) -> JobProgressDetail:
    doc_id_raw = data.get("xml_document_id", "")
    return JobProgressDetail(
        job_id=job_id,
        track_id=data.get("track_id", ""),
        current_step=_current_step(data),
        steps=JobSteps(
            downloaded=StepDownloaded(
                done=_parse_bool(data.get("downloaded_done", "0")),
                at=_or_none(data.get("downloaded_at", "")),
            ),
            xml_processed=StepXmlProcessed(
                done=_parse_bool(data.get("xml_done", "0")),
                at=_or_none(data.get("xml_at", "")),
                status=_or_none(data.get("xml_status", "")),
                document_id=int(doc_id_raw) if doc_id_raw else None,
                error=_or_none(data.get("xml_error", "")),
            ),
            accounting=StepAccounting(
                done=_parse_bool(data.get("accounting_done", "0")),
                at=_or_none(data.get("accounting_at", "")),
                status=_or_none(data.get("accounting_status", "")),
                error=_or_none(data.get("accounting_error", "")),
            ),
        ),
    )


class GetBatchStatusUseCase:
    def __init__(self, batch_store: RedisBatchStore, job_progress_store: JobProgressStore):
        self._store = batch_store
        self._progress = job_progress_store

    async def execute(self, batch_id: str, detail: bool = False) -> Optional[BatchStatusResponse]:
        data = await self._store.get(batch_id)
        if data is None:
            return None

        started_at = datetime.fromisoformat(data["started_at"])
        total = data["total"]
        job_ids: list = data["job_ids"]

        # Contadores por paso
        dl = StepSummary(done=0, pending=0, error=0)
        xml = StepSummary(done=0, pending=0, error=0)
        acc = StepSummary(done=0, pending=0, error=0)

        job_details = []
        all_finished = True
        auth_error_messages: list[str] = []

        for job_id in job_ids:
            progress = await self._progress.get(job_id)
            if progress is None:
                # Job sin registro de progreso aún — cuenta como pending en todo
                dl.pending += 1
                xml.pending += 1
                acc.pending += 1
                all_finished = False
                continue

            # downloaded
            if _parse_bool(progress.get("downloaded_done", "0")):
                dl.done += 1
            else:
                dl.pending += 1
                all_finished = False

            # xml_processed
            xml_done = _parse_bool(progress.get("xml_done", "0"))
            xml_status = progress.get("xml_status", "")
            xml_err = progress.get("xml_error", "")
            if xml_done:
                if xml_status == "error":
                    xml.error += 1
                    xml.errors.append(StepError(job_id=job_id, error=xml_err or "Error desconocido"))
                    if xml_err.startswith("AUTH_FAILED:"):
                        auth_error_messages.append(xml_err[len("AUTH_FAILED:") :].strip())
                else:
                    xml.done += 1
            else:
                xml.pending += 1
                all_finished = False

            # accounting — solo aplica si xml no tuvo error
            acc_done = _parse_bool(progress.get("accounting_done", "0"))
            acc_status = progress.get("accounting_status", "")
            acc_err = progress.get("accounting_error", "")
            if xml_status == "error":
                # No hay causación para XMLs con error — se descuenta del total
                pass
            elif acc_done:
                if acc_status == "error":
                    acc.error += 1
                    acc.errors.append(StepError(job_id=job_id, error=acc_err or "Error desconocido"))
                else:
                    acc.done += 1
            else:
                acc.pending += 1
                all_finished = False

            if detail:
                job_details.append(_build_job_detail(job_id, progress))

        now = datetime.now(timezone.utc)
        elapsed = round((now - started_at.replace(tzinfo=timezone.utc)).total_seconds(), 1)
        is_done = all_finished and total > 0

        # Superficie de error de auth: todos los jobs fallaron con AUTH_FAILED
        auth_error: Optional[str] = None
        if auth_error_messages and len(auth_error_messages) == total:
            auth_error = auth_error_messages[0]

        return BatchStatusResponse(
            batch_id=batch_id,
            total=total,
            elapsed_seconds=elapsed,
            total_time_seconds=elapsed if is_done else None,
            is_done=is_done,
            started_at=started_at.isoformat(),
            summary=BatchStepSummary(downloaded=dl, xml_processed=xml, accounting=acc),
            auth_error=auth_error,
            jobs=job_details if detail else None,
        )
