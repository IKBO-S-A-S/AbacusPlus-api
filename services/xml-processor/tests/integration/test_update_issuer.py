"""PATCH /issuers/{nit}: validaciones de tipo, relación con medios de pago y permisos."""

import pytest
from app.application.dto.issuer import NOTES_MAX_LENGTH, IssuerUpdateRequest
from app.application.use_cases.query_issuers import UpdateIssuerUseCase
from app.dependencies import get_update_issuer_use_case
from app.infrastructure.config.auth_dependency import TokenData, get_token_data
from app.infrastructure.persistence.models.integration_payment_type import IntegrationPaymentType
from app.infrastructure.persistence.models.issuer import Issuer
from app.infrastructure.persistence.repositories.issuer_repository import IssuerRepository
from app.main import app
from fastapi.testclient import TestClient
from pydantic import ValidationError
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

RUTA = "/api/v1/issuers/{}"


@pytest.fixture
def db():
    from app.infrastructure.config.database import Base

    # StaticPool + check_same_thread: el endpoint síncrono corre en otro hilo que el test.
    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    Base.metadata.create_all(engine)
    session = sessionmaker(bind=engine)()
    session.add_all(
        [
            Issuer(name="Alfa SAS", nit="900000001", dv=1, email="a@x.co"),
            Issuer(name="Beta SAS", nit="900000002", dv=2, email="b@x.co"),
            IntegrationPaymentType(id=1, name="Transferencia", type="bank", active=True),
            IntegrationPaymentType(id=2, name="Cheque", type="bank", active=False),
        ]
    )
    session.commit()
    yield session
    session.close()


@pytest.fixture
def client(db):
    app.dependency_overrides[get_update_issuer_use_case] = lambda: UpdateIssuerUseCase(
        IssuerRepository(db)
    )
    yield TestClient(app)
    app.dependency_overrides.clear()


def _autenticar(roles=("operator",)):
    app.dependency_overrides[get_token_data] = lambda: TokenData(
        {"sub": "1", "tenant_id": 1, "tenant_slug": "demo", "type": "access", "roles": list(roles)},
        "token",
    )


# --- DTO ---------------------------------------------------------------------------------


def test_tipo_contribuyente_se_normaliza():
    r = IssuerUpdateRequest(tipo_contribuyente=" o-13 ; O-48;o-13 ")
    assert r.tipo_contribuyente == "O-13;O-48"


def test_tipo_contribuyente_rechaza_texto_libre():
    with pytest.raises(ValidationError):
        IssuerUpdateRequest(tipo_contribuyente="Responsable de IVA")


def test_cuenta_solo_digitos():
    assert IssuerUpdateRequest(account_number=" 220505 ").account_number == "220505"
    with pytest.raises(ValidationError):
        IssuerUpdateRequest(account_number="2205-05")
    with pytest.raises(ValidationError):
        IssuerUpdateRequest(account_number="1" * 21)


def test_notas_se_normalizan_como_las_lee_el_llm_y_tienen_tope():
    assert IssuerUpdateRequest(notes="a\n\nb\t c").notes == "a b c"
    with pytest.raises(ValidationError):
        IssuerUpdateRequest(notes="x" * (NOTES_MAX_LENGTH + 1))


def test_nombre_y_correo_no_se_pueden_vaciar():
    for campo in ("name", "email"):
        with pytest.raises(ValidationError):
            IssuerUpdateRequest(**{campo: None})
        with pytest.raises(ValidationError):
            IssuerUpdateRequest(**{campo: "  "})
    with pytest.raises(ValidationError):
        IssuerUpdateRequest(email="sin-arroba")


# --- HTTP --------------------------------------------------------------------------------


def test_actualiza_parcialmente_y_devuelve_todas_las_columnas(client, db):
    _autenticar()
    r = client.patch(
        RUTA.format("900000001"),
        json={"account_number": "220505", "payment_id": 1, "notes": "Alquila equipos"},
    )
    assert r.status_code == 200
    body = r.json()
    assert body["account_number"] == "220505"
    assert body["payment_id"] == 1
    assert body["notes"] == "Alquila equipos"
    assert body["name"] == "Alfa SAS"  # lo no enviado no cambia
    assert set(body) == {c.name for c in Issuer.__table__.columns}


def test_null_desasigna_medio_de_pago_y_borra_notas(client, db):
    _autenticar()
    client.patch(RUTA.format("900000001"), json={"payment_id": 1, "notes": "algo"})
    r = client.patch(RUTA.format("900000001"), json={"payment_id": None, "notes": ""})
    assert r.status_code == 200
    assert r.json()["payment_id"] is None
    assert r.json()["notes"] is None


def test_medio_de_pago_inexistente_o_inactivo_es_422(client):
    _autenticar()
    assert client.patch(RUTA.format("900000001"), json={"payment_id": 99}).status_code == 422
    assert client.patch(RUTA.format("900000001"), json={"payment_id": 2}).status_code == 422


def test_modo_de_asignacion_se_define_y_se_deja_sin_definir(client):
    _autenticar()
    r = client.patch(RUTA.format("900000001"), json={"item_assignment_mode": "product"})
    assert r.status_code == 200
    assert r.json()["item_assignment_mode"] == "product"
    r = client.patch(RUTA.format("900000001"), json={"item_assignment_mode": "account"})
    assert r.json()["item_assignment_mode"] == "account"
    # `null` lo devuelve a «sin definir»: el sistema volverá a decidirlo con la próxima factura.
    r = client.patch(RUTA.format("900000001"), json={"item_assignment_mode": None})
    assert r.status_code == 200
    assert r.json()["item_assignment_mode"] is None


def test_modo_de_asignacion_rechaza_valores_desconocidos(client):
    _autenticar()
    r = client.patch(RUTA.format("900000001"), json={"item_assignment_mode": "inventario"})
    assert r.status_code == 422


def test_nombre_o_correo_de_otro_emisor_es_409(client):
    _autenticar()
    assert client.patch(RUTA.format("900000001"), json={"name": "Beta SAS"}).status_code == 409
    assert client.patch(RUTA.format("900000001"), json={"email": "B@x.co"}).status_code == 409


def test_conservar_el_propio_nombre_no_es_conflicto(client):
    _autenticar()
    assert client.patch(RUTA.format("900000001"), json={"name": "Alfa SAS"}).status_code == 200


def test_emisor_inexistente_es_404(client):
    _autenticar()
    assert client.patch(RUTA.format("1"), json={"notes": "x"}).status_code == 404


def test_body_vacio_y_nit_inmutable(client):
    _autenticar()
    assert client.patch(RUTA.format("900000001"), json={}).status_code == 422
    r = client.patch(RUTA.format("900000001"), json={"nit": "1", "notes": "x"})
    assert r.status_code == 200
    assert r.json()["nit"] == "900000001"


def test_viewer_no_escribe(client):
    _autenticar(roles=("viewer",))
    assert client.patch(RUTA.format("900000001"), json={"notes": "x"}).status_code == 403
