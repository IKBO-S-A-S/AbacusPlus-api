"""
Escribe el progreso de pasos xml_processed y accounting en Redis
para que session-proxy pueda consultarlo.

Comparte la misma estructura de hash que session-proxy/job_progress_store:
  job_progress:{job_id}
"""

import logging
from datetime import datetime, timezone
from typing import Optional

import redis.asyncio as aioredis

logger = logging.getLogger(__name__)

_TTL_SECONDS = 60 * 60 * 24 * 7  # 7 días


class JobProgressStore:
    def __init__(self, redis_url: str):
        self._redis_url = redis_url
        self._client: Optional[aioredis.Redis] = None

    async def _get_client(self) -> aioredis.Redis:
        if self._client is None:
            self._client = aioredis.from_url(self._redis_url, decode_responses=True)
        return self._client

    def _key(self, job_id: str) -> str:
        return f"job_progress:{job_id}"

    async def mark_xml_done(
        self,
        job_id: str,
        status: str,
        document_id: Optional[int] = None,
        error: Optional[str] = None,
        error_code: Optional[str] = None,
    ) -> None:
        client = await self._get_client()
        key = self._key(job_id)
        mapping = {
            "xml_done": "1",
            "xml_at": datetime.now(timezone.utc).isoformat(),
            "xml_status": status,
            "xml_document_id": str(document_id) if document_id else "",
            "xml_error": error or "",
            "xml_error_code": error_code or "",
        }
        await client.hset(key, mapping=mapping)
        await client.expire(key, _TTL_SECONDS)
        logger.debug("JobProgress xml_done — job_id=%s status=%s", job_id, status)

    async def mark_accounting_done(
        self,
        job_id: str,
        status: str,
        error: Optional[str] = None,
        error_code: Optional[str] = None,
    ) -> None:
        client = await self._get_client()
        key = self._key(job_id)
        mapping = {
            "accounting_done": "1",
            "accounting_at": datetime.now(timezone.utc).isoformat(),
            "accounting_status": status,
            "accounting_error": error or "",
            "accounting_error_code": error_code or "",
        }
        await client.hset(key, mapping=mapping)
        await client.expire(key, _TTL_SECONDS)
        logger.debug("JobProgress accounting_done — job_id=%s status=%s", job_id, status)
