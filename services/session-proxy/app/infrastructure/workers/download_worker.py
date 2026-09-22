"""
Worker ARQ para descarga de ZIPs del portal DIAN.

Ejecutar con:
    python -m arq app.infrastructure.workers.download_worker.WorkerSettings

Estrategia: un único job `download_batch` por lote abre un solo navegador Chromium,
autentica y "calienta" el WAF una vez, y descarga secuencialmente cada trackId con el
resolver adaptativo de BrowserDownloadSession. El progreso se lleva por trackId en Redis
(clave = track_id), de modo que el endpoint de estado del batch sigue funcionando.
"""

import contextlib
import logging
import os
import shutil
from datetime import datetime, timezone
from pathlib import Path

import httpx
from arq.connections import RedisSettings

from app.infrastructure.browser.playwright_client import (
    BrowserDownloadSession,
    DianDownloadError,
    is_valid_zip,
)
from app.infrastructure.config.logging import setup_logging
from app.infrastructure.queue.job_progress_store import JobProgressStore

setup_logging()
logger = logging.getLogger(__name__)


def _build_login_url() -> str:
    base_url = os.getenv("EXTERNAL_BASE_URL", "").rstrip("/")
    login_path = os.getenv("EXTERNAL_LOGIN_PATH", "/User/AuthToken")
    return f"{base_url}{login_path}"


def _build_auth_url(login_url: str, token: str, pk: str, rk: str) -> str:
    """Construye la URL de autenticación con token + pk/rk (fallback a env)."""
    effective_pk = pk or os.getenv("EXTERNAL_FIXED_PK", "")
    effective_rk = rk or os.getenv("EXTERNAL_FIXED_RK", "")
    params = f"token={token}"
    if effective_pk:
        params += f"&pk={effective_pk}"
    if effective_rk:
        params += f"&rk={effective_rk}"
    return f"{login_url}?{params}"


async def _trigger_xml_processing(filename: str, track_id: str, tenant_slug: str = "") -> None:
    """Llama a xml-processor para que procese el ZIP descargado (job_id = track_id).

    Este worker no tiene sesión de ningún usuario detrás — se despierta solo, encolado por
    ARQ — así que se autentica con `X-Internal-Secret` en vez de un bearer token. Sin este
    header el endpoint devuelve 401 y el ZIP queda descargado en disco pero nunca procesado,
    sin que nada más que un warning de texto lo delate.
    """
    xml_url = os.getenv("XML_PROCESSOR_URL", "http://xml-processor:8001")
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            response = await client.post(
                f"{xml_url}/api/v1/batch-jobs/file",
                json={"filename": filename, "job_id": track_id, "tenant_slug": tenant_slug},
                headers={"X-Internal-Secret": os.getenv("INTERNAL_SECRET", "")},
            )
        logger.info("batch-jobs/file [%s] → HTTP %d", filename, response.status_code)
    except Exception as e:
        logger.warning("No se pudo disparar procesamiento en xml-processor: %s", e)


async def _mark_downloaded(progress: JobProgressStore, track_id: str) -> None:
    redis = await progress._get_client()
    await redis.hset(
        progress._key(track_id),
        mapping={
            "downloaded_done": "1",
            "downloaded_at": datetime.now(timezone.utc).isoformat(),
        },
    )


async def _mark_failed(progress: JobProgressStore, track_id: str, error: str) -> None:
    """
    Marca un documento como terminado con error para que el batch no quede colgado
    en 'pending' indefinidamente. Reutiliza el paso xml_processed con status=error.
    """
    redis = await progress._get_client()
    now = datetime.now(timezone.utc).isoformat()
    await redis.hset(
        progress._key(track_id),
        mapping={
            "downloaded_done": "1",
            "downloaded_at": now,
            "xml_done": "1",
            "xml_at": now,
            "xml_status": "error",
            "xml_error": error[:500],
        },
    )


async def download_batch(
    ctx: dict,
    batch_id: str,
    track_ids: list,
    token: str,
    pk: str = "",
    rk: str = "",
    tenant_slug: str = "",
) -> dict:
    """
    Tarea ARQ: descarga todos los ZIPs de un lote reutilizando un solo navegador.
    Autentica y calienta el WAF una vez, luego descarga cada trackId con reintentos
    adaptativos. Un fallo por documento no aborta el lote completo.
    """
    progress: JobProgressStore = ctx["job_progress_store"]

    login_url = _build_login_url()
    downloads_dir = Path(os.getenv("DOWNLOADS_DIR", "/app/downloads"))
    downloads_dir.mkdir(parents=True, exist_ok=True)
    timeout_ms = int(os.getenv("BROWSER_TIMEOUT_MS", "60000"))
    max_retries = int(os.getenv("DOWNLOAD_MAX_RETRIES", "3"))

    auth_url = _build_auth_url(login_url, token, pk, rk)

    logger.info("Iniciando batch %s — %d documentos", batch_id, len(track_ids))
    downloaded = 0
    failed = 0

    try:
        session = BrowserDownloadSession(auth_url, timeout=timeout_ms, max_retries=max_retries)
        await session.open()
    except DianDownloadError as e:
        logger.error("Batch %s ABORTADO — auth fallida: %s", batch_id, e)
        for track_id in track_ids:
            await _mark_failed(progress, str(track_id), f"AUTH_FAILED: {e}")
        return {
            "batch_id": batch_id,
            "total": len(track_ids),
            "downloaded": 0,
            "failed": len(track_ids),
            "auth_error": str(e),
        }
    except Exception as e:
        logger.error("Batch %s ABORTADO — error inesperado en auth: %s", batch_id, e)
        for track_id in track_ids:
            await _mark_failed(progress, str(track_id), f"AUTH_ERROR: {type(e).__name__}: {e}")
        return {
            "batch_id": batch_id,
            "total": len(track_ids),
            "downloaded": 0,
            "failed": len(track_ids),
            "auth_error": str(e),
        }

    try:
        for track_id in track_ids:
            track_id = str(track_id)
            try:
                content = await session.download(track_id)
            except DianDownloadError as e:
                logger.warning("Documento %s no descargable: %s", track_id, e)
                await _mark_failed(progress, track_id, str(e))
                failed += 1
                # Chrome revienta cuando el perfil persistente acumula estado degradado
                # (cache/shaders de sesiones anteriores) tras muchos lanzamientos seguidos —
                # confirmado en vivo: el mismo batch pasó de 0/24 a 5/5 exitosos solo por
                # borrar el perfil y arrancar de cero. Relanzar reutilizando el MISMO
                # directorio de perfil no arregla nada porque el estado corrupto sigue ahí;
                # hay que borrarlo antes de abrir la sesión nueva.
                if not session.is_alive():
                    logger.warning(
                        "Sesión de navegador caída tras %s — perfil probablemente degradado, "
                        "borrando y relanzando para continuar el batch",
                        track_id,
                    )
                    stale_profile_dir = session.profile_dir
                    with contextlib.suppress(Exception):
                        await session.close()
                    shutil.rmtree(stale_profile_dir, ignore_errors=True)
                    try:
                        session = BrowserDownloadSession(
                            auth_url, timeout=timeout_ms, max_retries=max_retries
                        )
                        await session.open()
                    except Exception as reopen_exc:
                        logger.error(
                            "No se pudo relanzar la sesión tras crash — abortando resto del batch: %s",
                            reopen_exc,
                        )
                        remaining = track_ids[track_ids.index(track_id) + 1 :]
                        for pending_id in remaining:
                            await _mark_failed(
                                progress, str(pending_id), f"BROWSER_RELAUNCH_FAILED: {reopen_exc}"
                            )
                        failed += len(remaining)
                        break
                continue
            except Exception as e:
                logger.error("Error inesperado descargando %s: %s", track_id, e)
                await _mark_failed(progress, track_id, f"{type(e).__name__}: {e}")
                failed += 1
                continue

            if not is_valid_zip(content):
                msg = "Contenido descargado no es un ZIP válido (firma PK ausente)"
                logger.warning("Documento %s: %s", track_id, msg)
                await _mark_failed(progress, track_id, msg)
                failed += 1
                continue

            filename = f"{track_id}.zip"
            file_path = downloads_dir / filename
            file_path.write_bytes(content)
            logger.info("ZIP guardado: %s (%d bytes)", file_path, len(content))

            await _mark_downloaded(progress, track_id)
            await _trigger_xml_processing(filename, track_id, tenant_slug)
            downloaded += 1
    finally:
        await session.close()

    logger.info("Batch %s terminado — descargados: %d, fallidos: %d", batch_id, downloaded, failed)
    return {
        "batch_id": batch_id,
        "total": len(track_ids),
        "downloaded": downloaded,
        "failed": failed,
    }


async def on_startup(ctx: dict) -> None:
    ctx["job_progress_store"] = JobProgressStore(
        redis_url=os.getenv("REDIS_URL", "redis://redis:6379")
    )
    logger.info("Worker ARQ iniciado — descarga por lote con navegador reutilizado")


async def on_shutdown(ctx: dict) -> None:
    logger.info("Worker ARQ apagándose")


class WorkerSettings:
    functions = [download_batch]
    on_startup = on_startup
    on_shutdown = on_shutdown
    redis_settings = RedisSettings.from_dsn(os.getenv("REDIS_URL", "redis://redis:6379"))
    # 1, no más: BrowserDownloadSession usa un directorio de perfil de Chrome FIJO y
    # persistente (para conservar la cookie cf_clearance entre ejecuciones). Con
    # max_jobs=2 dos batches concurrentes abren `launch_persistent_context` sobre el
    # mismo directorio a la vez — Chrome rechaza el segundo por el lock ProcessSingleton
    # y ambas sesiones terminan con las páginas cerradas a medio descargar
    # ("Target page, context or browser has been closed") en vez de fallar limpio.
    max_jobs = 1
    job_timeout = 1200
    keep_result = 3600
    max_tries = 2
