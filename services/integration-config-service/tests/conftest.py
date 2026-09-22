import os

import pytest
from cryptography.fernet import Fernet
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

os.environ.setdefault("DATABASE_HOST", "localhost")
os.environ.setdefault("DATABASE_PORT", "5432")
os.environ.setdefault("DATABASE_USER", "test")
os.environ.setdefault("DATABASE_PASSWORD", "test")
os.environ.setdefault("DATABASE_NAME", "test")
os.environ.setdefault("STORAGE_SECRETS_KEY", Fernet.generate_key().decode())


@pytest.fixture
def db_session():
    """Crea una sesion SQLite en memoria para tests, con todas las tablas registradas."""
    import app.infrastructure.persistence.models.integration  # noqa: F401
    import app.infrastructure.persistence.models.storage  # noqa: F401
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
