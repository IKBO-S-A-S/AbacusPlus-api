import os

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

# Set env vars before importing any app module
os.environ.setdefault("DATABASE_HOST", "localhost")
os.environ.setdefault("DATABASE_PORT", "5432")
os.environ.setdefault("DATABASE_USER", "test")
os.environ.setdefault("DATABASE_PASSWORD", "test")
os.environ.setdefault("DATABASE_NAME", "test")
os.environ.setdefault("OLLAMA_HOST", "http://localhost:11434")
os.environ.setdefault("OLLAMA_EMBED_MODEL", "nomic-embed-text")

# Los modelos se importan a nivel de MÓDULO y no dentro de la fixture.
#
# SQLAlchemy resuelve las relaciones por nombre de clase, y solo puede hacerlo cuando todas
# las clases implicadas están registradas. Configurar los mappers lo dispara la primera
# consulta o la primera relación que se recorra, y eso puede ocurrir al importar cualquier
# módulo de la aplicación — mucho antes de que una fixture llegue a ejecutarse.
#
# Con los imports dentro de la fixture, el registro dependía de que el test pidiera
# `db_session`. Un test que solo importara un router fallaba con «expression 'DocumentTax'
# failed to locate a name», y el mensaje no apunta en absoluto a la causa real. Registrarlos
# aquí arriba hace que el orden deje de importar.
import app.infrastructure.persistence.models.accounting  # noqa: E402,F401
import app.infrastructure.persistence.models.concept  # noqa: E402,F401
import app.infrastructure.persistence.models.document  # noqa: E402,F401
import app.infrastructure.persistence.models.document_tax  # noqa: E402,F401
import app.infrastructure.persistence.models.integration_cost_center  # noqa: E402,F401
import app.infrastructure.persistence.models.integration_payment_type  # noqa: E402,F401
import app.infrastructure.persistence.models.integration_product  # noqa: E402,F401
import app.infrastructure.persistence.models.integration_retention  # noqa: E402,F401
import app.infrastructure.persistence.models.integration_tax  # noqa: E402,F401
import app.infrastructure.persistence.models.issuer  # noqa: E402,F401
import app.infrastructure.persistence.models.receiver  # noqa: E402,F401
import app.infrastructure.persistence.models.tax  # noqa: E402,F401


@pytest.fixture
def db_session():
    """Create an in-memory SQLite session for tests."""
    from app.infrastructure.config.database import Base

    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine)
    session = Session()
    try:
        yield session
    finally:
        session.close()
        Base.metadata.drop_all(engine)
