from datetime import datetime, timezone

from sqlalchemy import Column, DateTime, Integer, String

from app.infrastructure.config.database import Base


class Receiver(Base):
    __tablename__ = "receivers"

    id = Column(Integer, primary_key=True, index=True)
    name = Column(String(255), unique=True, index=True, nullable=False)
    nit = Column(String(50), unique=True, index=True, nullable=False)
    dv = Column(Integer, nullable=False)
    phone = Column(String(100), nullable=True)
    email = Column(String(255), unique=True, index=True, nullable=False)
    template = Column(String(50), nullable=True)
    created_at = Column(DateTime, default=datetime.now(timezone.utc))
