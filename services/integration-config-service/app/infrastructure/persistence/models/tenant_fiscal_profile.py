from sqlalchemy import Boolean, Column, DateTime, Integer, String, func

from app.infrastructure.config.database import Base


class TenantFiscalProfile(Base):
    """Perfil fiscal de la empresa (tenant) — el COMPRADOR en las facturas de compra.

    Es un singleton por base de tenant: describe la condición tributaria propia de la empresa
    que usa Abacus. Determina si la empresa RETIENE (agente de retención) y bajo qué reglas,
    y es AUTORITATIVO sobre lo que trae el XML: el `TaxLevelCode` del receptor en la factura
    puede venir incompleto, así que el contador confirma aquí el perfil real y este manda.

    Se guarda una sola fila (id fijo = 1); el repositorio hace upsert sobre ella.
    """

    __tablename__ = "tenant_fiscal_profile"

    id = Column(Integer, primary_key=True)
    # NIT de la empresa, solo dígitos y sin dígito de verificación (p. ej. '900031352'). Se
    # guarda como texto, igual que `issuers.nit` y `receivers.nit` en xml-processor, para poder
    # compararlo tal cual con el NIT del receptor de la factura. Nullable: los perfiles
    # existentes no lo tienen hasta que el contador lo diligencie.
    nit = Column(String(20), nullable=True)
    # ── Condición como AGENTE de retención (define si la empresa retiene) ──
    agente_retencion_renta = Column(Boolean, nullable=False, default=False)
    agente_retencion_ica = Column(Boolean, nullable=False, default=False)
    agente_retencion_iva = Column(Boolean, nullable=False, default=False)
    # ── Otras condiciones que afectan la procedencia/tarifa ──
    autorretenedor_renta = Column(Boolean, nullable=False, default=False)
    gran_contribuyente = Column(Boolean, nullable=False, default=False)
    responsable_iva = Column(Boolean, nullable=False, default=False)
    # 'ordinario' | 'simple' (RST). El régimen simple cambia las reglas de retención.
    regimen = Column(String(20), nullable=False, default="ordinario")
    # Los municipios donde la empresa retiene ICA NO se guardan aquí: son los de la tabla
    # `retention_ica_rates` (xml-processor), que es la única que además lleva la TARIFA. Sin
    # tarifa no se puede proponer ReteICA, así que un municipio listado aparte solo podía
    # coincidir con esa tabla —y ser redundante— o contradecirla —y desorientar al modelo.
    notas = Column(String(500), nullable=True)
    updated_at = Column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )
