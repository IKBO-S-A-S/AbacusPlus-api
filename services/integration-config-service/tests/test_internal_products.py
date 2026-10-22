"""El catálogo de productos, accesible servicio-a-servicio (llm-service).

Mismo patrón y mismo motivo que `/internal/taxes`: la asignación automática corre en segundo
plano, sin JWT de usuario, así que se autentica con `X-Internal-Secret` y elige la base con
`X-Tenant-Slug`.
"""

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.infrastructure.persistence.repositories.product_repository import ProductRepository


@pytest.fixture
def client(monkeypatch):
    monkeypatch.setenv("INTERNAL_SECRET", "secreto-de-prueba")
    from app.main import app

    return TestClient(app)


def test_rechaza_peticion_sin_secreto_interno(client):
    respuesta = client.get("/internal/products", headers={"X-Tenant-Slug": "ikbo"})
    assert respuesta.status_code in (401, 403, 422)


def test_rechaza_secreto_interno_incorrecto(client):
    respuesta = client.get(
        "/internal/products", headers={"X-Internal-Secret": "otro", "X-Tenant-Slug": "ikbo"}
    )
    assert respuesta.status_code in (401, 403)


def test_exige_el_tenant(client):
    respuesta = client.get("/internal/products", headers={"X-Internal-Secret": "secreto-de-prueba"})
    assert respuesta.status_code == 422


def test_la_ruta_esta_registrada(client):
    # No se llama con secreto válido: resolvería la base del tenant y este test no debe
    # depender de PostgreSQL.
    assert "/internal/products" in {getattr(r, "path", None) for r in client.app.routes}


@pytest.fixture
def repo():
    from app.infrastructure.config.database import Base

    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    Base.metadata.create_all(engine)
    session = sessionmaker(bind=engine)()
    repo = ProductRepository(session)
    repo.upsert_many(
        [
            {"code": "P-001", "type": "product", "description": "Tornillo", "active": True},
            {"code": "P-OFF", "type": "product", "description": "Viejo", "active": False},
            {"code": "S-001", "type": "service", "description": "Asesoría", "active": True},
        ]
    )
    yield repo
    session.close()


def test_filtra_por_tipo_y_estado_en_el_repositorio(repo):
    assert [p.code for p in repo.list(active=True, type="product")] == ["P-001"]
    assert [p.code for p in repo.list(type=" Service ")] == ["S-001"]
    assert len(repo.list()) == 3
