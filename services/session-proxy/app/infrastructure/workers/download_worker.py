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
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

import httpx
from arq.connections import RedisSettings

from app.infrastructure.browser.playwright_client import (
    BrowserDownloadSession,
    DianDownloadError,
    is_valid_zip,
)
from app.infrastructure.config.logging import setup_logging
from app.infrastructure.metrics import log_metric, timed
from app.infrastructure.queue.job_progress_store import JobProgressStore

setup_logging()
logger = logging.getLogger(__name__)


def _log_doc_metric(
    batch_id: str, track_id: str, outcome: str, started: float, timings: dict, zip_bytes: int = 0
) -> None:
    """Una línea por documento con el desglose de tiempos de su descarga."""
    log_metric(
        "doc_download",
        batch=batch_id,
        track=track_id[:16],
        outcome=outcome,
        total_ms=round((time.perf_counter() - started) * 1000),
        turnstile_ms=timings.get("turnstile_ms", 0),
        nav_ms=timings.get("nav_ms", 0),
        fetch_ms=timings.get("fetch_ms", 0),
        backoff_ms=timings.get("backoff_ms", 0),
        save_ms=timings.get("save_ms", 0),
        notify_ms=timings.get("notify_ms", 0),
        attempts=timings.get("attempts", 0),
        path=timings.get("path"),
        zip_bytes=zip_bytes,
    )


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
            "download_status": "ok",
        },
    )


async def _mark_failed(
    progress: JobProgressStore, track_id: str, error: str, error_code: str = "UNKNOWN"
) -> None:
    """
    Marca un documento como terminado con error para que el batch no quede colgado
    en 'pending' indefinidamente.

    `download_status`/`download_error_code` son el registro real de que la descarga (no el
    procesamiento del XML) fue lo que falló, y con qué motivo específico —DIAN_INVALID,
    AUTH_LOST, FETCH_ERROR, etc., el mismo `kind` que ya traía `DianDownloadError` y que antes
    se descartaba al convertir la excepción en texto plano. `xml_done`/`xml_status=error` se
    conservan además por compatibilidad: son los que usa `is_done` para no dejar el batch
    colgado, aunque el XML nunca haya llegado a procesarse.
    """
    redis = await progress._get_client()
    now = datetime.now(timezone.utc).isoformat()
    await redis.hset(
        progress._key(track_id),
        mapping={
            "downloaded_done": "1",
            "downloaded_at": now,
            "download_status": "error",
            "download_error": error[:500],
            "download_error_code": error_code,
            "xml_done": "1",
            "xml_at": now,
            "xml_status": "error",
            "xml_error": error[:500],
        },
    )


async def _relaunch_session(
    session: BrowserDownloadSession, auth_url: str, timeout_ms: int, max_retries: int
) -> tuple[BrowserDownloadSession, Optional[Exception]]:
    """Cierra la sesión caída, borra su perfil y abre una nueva.

    Devuelve `(sesión, None)` si funcionó, o `(sesión_anterior, error)` si no se pudo abrir.

    Chrome revienta cuando el perfil persistente acumula estado degradado (cache/shaders de
    sesiones anteriores) tras muchos lanzamientos seguidos — confirmado en vivo: el mismo
    batch pasó de 0/24 a 5/5 exitosos solo por borrar el perfil y arrancar de cero. Relanzar
    reutilizando el MISMO directorio de perfil no arregla nada porque el estado corrupto sigue
    ahí; hay que borrarlo antes de abrir la sesión nueva.
    """
    stale_profile_dir = session.profile_dir
    with contextlib.suppress(Exception):
        await session.close()
    shutil.rmtree(stale_profile_dir, ignore_errors=True)
    try:
        new_session = BrowserDownloadSession(auth_url, timeout=timeout_ms, max_retries=max_retries)
        await new_session.open()
        return new_session, None
    except Exception as exc:
        return session, exc


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
    batch_started = time.perf_counter()
    open_timings: dict = {}

    try:
        session = BrowserDownloadSession(auth_url, timeout=timeout_ms, max_retries=max_retries)
        with timed("open_ms", open_timings):
            await session.open()
    except DianDownloadError as e:
        logger.error("Batch %s ABORTADO — auth fallida: %s", batch_id, e)
        for track_id in track_ids:
            await _mark_failed(progress, str(track_id), f"AUTH_FAILED: {e}", error_code="AUTH_FAILED")
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
            await _mark_failed(
                progress, str(track_id), f"AUTH_ERROR: {type(e).__name__}: {e}", error_code="AUTH_ERROR"
            )
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
            doc_started = time.perf_counter()
            timings: dict = {}
            content = None
            abort_batch = False
            # Dos pasadas como máximo: la segunda solo existe para reintentar un documento
            # cuyo fallo fue que Chrome murió, no el documento en sí.
            for pass_no in (1, 2):
                try:
                    content = await session.download(track_id, timings=timings)
                    break
                except DianDownloadError as e:
                    logger.warning("Documento %s no descargable: %s", track_id, e)
                    crashed = not session.is_alive()
                    relaunch_error: Optional[Exception] = None
                    if crashed:
                        logger.warning(
                            "Sesión de navegador caída tras %s — perfil probablemente degradado, "
                            "borrando y relanzando para continuar el batch",
                            track_id,
                        )
                        session, relaunch_error = await _relaunch_session(
                            session, auth_url, timeout_ms, max_retries
                        )
                    if crashed and relaunch_error is None and pass_no == 1:
                        # El documento no tuvo la culpa: se reintenta con el navegador nuevo.
                        # Antes quedaba marcado como fallido aunque sí existía en la DIAN.
                        logger.info("Reintentando %s con el navegador nuevo", track_id)
                        continue
                    _log_doc_metric(batch_id, track_id, e.kind, doc_started, timings)
                    # `e.kind` ya trae el motivo específico (DIAN_INVALID, AUTH_LOST,
                    # FETCH_ERROR, CLOUDFLARE, AZURE_WAF, BROWSER_CRASHED...) — antes se perdía
                    # al convertir la excepción en texto plano, dejando al usuario sin forma de
                    # distinguir un documento que no existe en la DIAN de una caída transitoria.
                    await _mark_failed(progress, track_id, str(e), error_code=e.kind)
                    failed += 1
                    if relaunch_error is not None:
                        logger.error(
                            "No se pudo relanzar la sesión tras crash — abortando resto del batch: %s",
                            relaunch_error,
                        )
                        remaining = track_ids[track_ids.index(track_id) + 1 :]
                        for pending_id in remaining:
                            await _mark_failed(
                                progress,
                                str(pending_id),
                                f"BROWSER_RELAUNCH_FAILED: {relaunch_error}",
                                error_code="BROWSER_RELAUNCH_FAILED",
                            )
                        failed += len(remaining)
                        abort_batch = True
                    break
                except Exception as e:
                    logger.error("Error inesperado descargando %s: %s", track_id, e)
                    _log_doc_metric(batch_id, track_id, type(e).__name__, doc_started, timings)
                    await _mark_failed(
                        progress, track_id, f"{type(e).__name__}: {e}", error_code=type(e).__name__
                    )
                    failed += 1
                    break
            if abort_batch:
                break
            if content is None:
                continue

            if not is_valid_zip(content):
                msg = "Contenido descargado no es un ZIP válido (firma PK ausente)"
                logger.warning("Documento %s: %s", track_id, msg)
                _log_doc_metric(batch_id, track_id, "INVALID_ZIP", doc_started, timings)
                await _mark_failed(progress, track_id, msg, error_code="INVALID_ZIP")
                failed += 1
                continue

            filename = f"{track_id}.zip"
            file_path = downloads_dir / filename
            with timed("save_ms", timings):
                file_path.write_bytes(content)
            logger.info("ZIP guardado: %s (%d bytes)", file_path, len(content))

            await _mark_downloaded(progress, track_id)
            with timed("notify_ms", timings):
                await _trigger_xml_processing(filename, track_id, tenant_slug)
            downloaded += 1
            _log_doc_metric(batch_id, track_id, "ok", doc_started, timings, len(content))
    finally:
        await session.close()

    batch_ms = round((time.perf_counter() - batch_started) * 1000)
    log_metric(
        "batch_download",
        batch=batch_id,
        docs=len(track_ids),
        ok=downloaded,
        failed=failed,
        open_ms=open_timings.get("open_ms", 0),
        total_ms=batch_ms,
        avg_ms_per_doc=round(batch_ms / len(track_ids)) if track_ids else 0,
    )
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
