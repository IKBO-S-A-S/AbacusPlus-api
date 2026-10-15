import logging
import os
import re

import httpx
import psycopg2
from sqlalchemy import NullPool, create_engine

from app.infrastructure.config.tenant_connection import get_session_for_tenant
from app.infrastructure.persistence.models.user import TenantBase
from app.infrastructure.persistence.repositories.user_repository import UserRepository

logger = logging.getLogger(__name__)

_VALID_SLUG = re.compile(r"^[a-z][a-z0-9_]{1,30}$")

# Orden significativo: integration-config-service primero, porque dueño de las tablas
# `integration_*` que xml-processor referencia con FK (mismo orden que provision-tenants.sh).
SERVICE_URLS = [
    os.getenv("INTEGRATION_CONFIG_URL", "http://integration-config-service:8007"),
    os.getenv("XML_PROCESSOR_URL", "http://xml-processor:8001"),
    os.getenv("RAG_SERVICE_URL", "http://rag-service:8002"),
    os.getenv("LLM_SERVICE_URL", "http://llm-service:8003"),
    os.getenv("SIIGO_SERVICE_URL", "http://siigo-service:8006"),
]


def validate_slug(slug: str) -> None:
    if not _VALID_SLUG.match(slug):
        raise ValueError(f"Invalid slug '{slug}': must match [a-z][a-z0-9_]{{1,30}}")


def provision(tenant_slug: str, admin_email: str, admin_password_hash: str) -> None:
    # Defensa en profundidad: el slug se interpola en `CREATE DATABASE "..."` y en el DSN,
    # así que se re-valida aquí aunque el caller ya lo haya validado. Es la única barrera si
    # se invoca provision() desde otra ruta en el futuro.
    validate_slug(tenant_slug)
    _create_database(tenant_slug)
    _notify_services(tenant_slug)
    _create_admin_user(tenant_slug, admin_email, admin_password_hash)


def _get_admin_conn(dbname: str):
    return psycopg2.connect(
        host=os.environ["DATABASE_HOST"],
        port=int(os.environ.get("DATABASE_PORT", "5432")),
        user=os.environ["DATABASE_USER"],
        password=os.environ["DATABASE_PASSWORD"],
        dbname=dbname,
    )


def _create_database(tenant_slug: str) -> None:
    db_name = f"abacus_t_{tenant_slug}"
    conn = _get_admin_conn("abacus_meta")
    conn.autocommit = True  # CREATE DATABASE cannot run inside a transaction
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT 1 FROM pg_database WHERE datname = %s", (db_name,))
            if cur.fetchone() is None:
                cur.execute(f'CREATE DATABASE "{db_name}"')
                logger.info("Created database %s", db_name)
            else:
                logger.info("Database %s already exists, skipping creation", db_name)
    finally:
        conn.close()

    conn2 = _get_admin_conn(db_name)
    conn2.autocommit = True
    try:
        with conn2.cursor() as cur:
            cur.execute("CREATE EXTENSION IF NOT EXISTS vector")
        logger.info("vector extension ready in %s", db_name)
    finally:
        conn2.close()

    # Create User/UserRole tables in the new tenant DB
    user = os.environ["DATABASE_USER"]
    password = os.environ["DATABASE_PASSWORD"]
    host = os.environ["DATABASE_HOST"]
    port = os.environ.get("DATABASE_PORT", "5432")
    url = f"postgresql+psycopg2://{user}:{password}@{host}:{port}/{db_name}"
    engine = create_engine(url, poolclass=NullPool)
    TenantBase.metadata.create_all(bind=engine, checkfirst=True)
    engine.dispose()
    logger.info("User/UserRole tables created in %s", db_name)


def _notify_services(tenant_slug: str) -> None:
    secret = os.environ.get("INTERNAL_SECRET", "")
    headers = {"x-internal-secret": secret}
    for url in SERVICE_URLS:
        if not url:
            continue
        try:
            resp = httpx.post(
                f"{url}/internal/provision-tenant",
                params={"tenant_slug": tenant_slug},
                headers=headers,
                timeout=30.0,
            )
            resp.raise_for_status()
            logger.info("Provisioned %s on %s", tenant_slug, url)
        except Exception as exc:
            logger.error("Failed to provision %s on %s: %s", tenant_slug, url, exc)
            raise RuntimeError(f"Provision failed for service {url}: {exc}") from exc


def _create_admin_user(tenant_slug: str, email: str, password_hash: str) -> None:
    db = get_session_for_tenant(tenant_slug)
    try:
        repo = UserRepository(db)
        existing = repo.get_by_email(email)
        if existing is None:
            user = repo.create(email=email, password_hash=password_hash)
            repo.assign_role(user.id, "tenant_admin")
            db.commit()
            logger.info("Admin user %s created in tenant %s", email, tenant_slug)
        else:
            logger.info("Admin user %s already exists in tenant %s", email, tenant_slug)
    finally:
        db.close()
