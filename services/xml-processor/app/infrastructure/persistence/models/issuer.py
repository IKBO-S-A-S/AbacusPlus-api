from datetime import datetime, timezone

from sqlalchemy import Column, DateTime, ForeignKey, Integer, String

from app.infrastructure.config.database import Base


class Issuer(Base):
    __tablename__ = "issuers"

    id = Column(Integer, primary_key=True, index=True)
    name = Column(String(255), unique=True, index=True, nullable=False)
    nit = Column(String(50), unique=True, index=True, nullable=False)
    dv = Column(Integer, nullable=False)
    phone = Column(String(100), nullable=True)
    email = Column(String(255), unique=True, index=True, nullable=False)
    account_number = Column(String(50), nullable=True, default="")
    # Tipo de contribuyente / régimen (p.ej. "Responsable de IVA", "No responsable", etc.)
    tipo_contribuyente = Column(String(255), nullable=True)
    payment_id = Column(
        Integer, ForeignKey("integration_payment_types.id"), nullable=True, default=None
    )
    # Reglas contables específicas de este proveedor para el LLM (texto libre).
    # Ejemplo: "Este proveedor alquila equipos. Usar cuenta 513535 para todas sus facturas."
    notes = Column(String(500), nullable=True)
    created_at = Column(DateTime, default=datetime.now(timezone.utc))
