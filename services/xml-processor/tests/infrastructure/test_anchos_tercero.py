"""Los datos del tercero caben en `issuers`/`receivers`.

Un emisor con razón social de 56 caracteres ("PUBLICAR PUBLICIDAD MULTIMEDIA S.A.S - EN
REORGANIZACIÓN") desbordaba `issuers.name` (50) y el documento no se procesaba.
"""

import pytest
from app.infrastructure.persistence.models.document import Document
from app.infrastructure.persistence.models.issuer import Issuer
from app.infrastructure.persistence.models.receiver import Receiver
from app.infrastructure.persistence.tenant_migrations import TENANT_MIGRATIONS, _ampliar_varchar

NOMBRE_REAL = "PUBLICAR PUBLICIDAD MULTIMEDIA S.A.S - EN REORGANIZACIÓN"


def _largo(modelo, columna):
    return modelo.__table__.c[columna].type.length


@pytest.mark.parametrize(
    "modelo, columna, minimo",
    [
        (Issuer, "name", 255),
        (Issuer, "email", 255),
        (Issuer, "phone", 100),
        (Issuer, "tipo_contribuyente", 255),
        (Receiver, "name", 255),
        (Receiver, "email", 255),
        (Receiver, "phone", 100),
        (Document, "receiver_responsibilities", 255),
    ],
)
def test_los_modelos_admiten_los_anchos_de_documents(modelo, columna, minimo):
    assert _largo(modelo, columna) >= minimo


def test_el_nombre_real_que_fallo_cabe():
    assert len(NOMBRE_REAL) > 50
    assert len(NOMBRE_REAL) <= _largo(Issuer, "name")


def test_la_migracion_ensancha_cada_columna_de_los_modelos():
    sentencias = " ".join(TENANT_MIGRATIONS)
    for tabla, columna in [
        ("issuers", "name"),
        ("issuers", "email"),
        ("issuers", "phone"),
        ("issuers", "tipo_contribuyente"),
        ("receivers", "name"),
        ("receivers", "email"),
        ("receivers", "phone"),
        ("documents", "receiver_responsibilities"),
    ]:
        assert f"ALTER TABLE {tabla} ALTER COLUMN {columna} TYPE" in sentencias


def test_la_sentencia_solo_ensancha_nunca_estrecha():
    sql = _ampliar_varchar("issuers", "name", 255)

    assert "< 255" in sql  # solo actúa si la columna es más angosta
    assert "TYPE VARCHAR(255)" in sql
