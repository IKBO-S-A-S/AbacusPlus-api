import os

from dotenv import load_dotenv

from app.application.use_cases.company_login import CompanyLoginUseCase
from app.application.use_cases.fetch_and_enqueue_documents import FetchAndEnqueueDocumentsUseCase
from app.application.use_cases.get_batch_status import GetBatchStatusUseCase
from app.application.use_cases.get_job_status import GetJobStatusUseCase
from app.application.use_cases.login import LoginUseCase
from app.application.use_cases.proxy_request import ProxyRequestUseCase
from app.infrastructure.browser.playwright_client import PlaywrightBrowserClient
from app.infrastructure.clients.external_client import HttpxExternalClient
from app.infrastructure.clients.fiscal_profile_client import HttpFiscalProfileClient
from app.infrastructure.queue.arq_queue import ArqJobQueue
from app.infrastructure.queue.batch_store import RedisBatchStore
from app.infrastructure.queue.job_progress_store import JobProgressStore
from app.infrastructure.session.in_memory_store import InMemorySessionStore

load_dotenv()

# Singleton: se crea una sola vez al importar este módulo.
# Todos los request handlers comparten esta instancia.
_SESSION_STORE = InMemorySessionStore(ttl_seconds=int(os.getenv("SESSION_TTL_SECONDS", "3600")))


def get_session_store() -> InMemorySessionStore:
    return _SESSION_STORE


def get_external_client() -> HttpxExternalClient:
    return HttpxExternalClient(timeout=15.0)


def _build_login_url() -> str:
    base = os.getenv("EXTERNAL_BASE_URL", "https://portal.example.com").rstrip("/")
    path = os.getenv("EXTERNAL_LOGIN_PATH", "/api/login")
    return f"{base}{path}"


def get_login_use_case() -> LoginUseCase:
    return LoginUseCase(
        session_store=get_session_store(),
        external_client=get_external_client(),
        login_url=_build_login_url(),
    )


def get_proxy_request_use_case() -> ProxyRequestUseCase:
    return ProxyRequestUseCase(
        external_client=get_external_client(),
        base_url=os.getenv("EXTERNAL_BASE_URL", "https://portal.example.com"),
        login_url=_build_login_url(),
    )


def get_browser_client() -> PlaywrightBrowserClient:
    return PlaywrightBrowserClient(
        representative=os.getenv("REPRESENTATIVE", ""),
        nit=os.getenv("NIT", ""),
        timeout=int(os.getenv("BROWSER_TIMEOUT_MS", "60000")),
    )


def get_company_login_use_case() -> CompanyLoginUseCase:
    return CompanyLoginUseCase(
        session_store=get_session_store(),
        browser_client=get_browser_client(),
        login_url=os.getenv("EXTERNAL_BASE_URL", "").rstrip("/") + "/User/CompanyLogin",
    )


def get_job_queue() -> ArqJobQueue:
    return ArqJobQueue(redis_url=os.getenv("REDIS_URL", "redis://redis:6379"))


def get_batch_store() -> RedisBatchStore:
    return RedisBatchStore(redis_url=os.getenv("REDIS_URL", "redis://redis:6379"))


def get_job_progress_store() -> JobProgressStore:
    return JobProgressStore(redis_url=os.getenv("REDIS_URL", "redis://redis:6379"))


def get_fetch_and_enqueue_use_case() -> FetchAndEnqueueDocumentsUseCase:
    return FetchAndEnqueueDocumentsUseCase(
        external_client=get_external_client(),
        job_queue=get_job_queue(),
        base_url=os.getenv("EXTERNAL_BASE_URL", ""),
        login_url=_build_login_url(),
        batch_store=get_batch_store(),
        job_progress_store=get_job_progress_store(),
        fiscal_profile=HttpFiscalProfileClient(),
    )


def get_job_status_use_case() -> GetJobStatusUseCase:
    return GetJobStatusUseCase(job_queue=get_job_queue())


def get_batch_status_use_case() -> GetBatchStatusUseCase:
    return GetBatchStatusUseCase(
        batch_store=get_batch_store(),
        job_progress_store=get_job_progress_store(),
    )
