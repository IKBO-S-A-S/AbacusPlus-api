import re
from datetime import datetime
from typing import Optional

from pydantic import BaseModel, ConfigDict, Field, field_validator

_CONTROL_CHARS = re.compile(r"[\x00-\x1f\x7f]")


class IssuerResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int = Field(description="Identificador interno del emisor.", examples=[1])
    name: str = Field(description="Razón social.", examples=["Proveedor SAS"])
    nit: str = Field(description="NIT sin dígito de verificación.", examples=["900123456"])
    dv: int = Field(description="Dígito de verificación del NIT.", examples=[7])
    phone: Optional[str] = Field(None, description="Teléfono.", examples=["3001234567"])
    email: Optional[str] = Field(None, description="Correo.", examples=["ventas@proveedor.co"])
    account_number: Optional[str] = Field(
        None, description="Cuenta contable de cuentas por pagar.", examples=["220505"]
    )
    tipo_contribuyente: Optional[str] = Field(
        None, description="Régimen / tipo de contribuyente.", examples=["Responsable de IVA"]
    )
    payment_id: Optional[int] = Field(
        None, description="Medio de pago del emisor (`integration_payment_types.id`).", examples=[3]
    )
    notes: Optional[str] = Field(
        None, description="Reglas contables específicas del proveedor para el LLM."
    )
    created_at: Optional[datetime] = Field(None, description="Fecha de creación del registro.")


#: Tope de `notes`. La columna admite 500, pero el llm-service solo lee los primeros 200
#: caracteres (`_MAX_ISSUER_TEXT` en suggest_retentions.py): aceptar más sería guardar texto que
#: el modelo nunca ve. Si ese tope cambia allá, cambia aquí.
NOTES_MAX_LENGTH = 200

#: Código de responsabilidad fiscal de la DIAN (lista 48): «O-13», «R-99-PN»…
_RESPONSIBILITY_CODE = re.compile(r"^[A-Z]-\d{1,3}(-[A-Z]{1,3})?$")
_EMAIL = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
_WHITESPACE = re.compile(r"\s+")


class IssuerUpdateRequest(BaseModel):
    """Campos editables del emisor. Todos opcionales: solo se modifica lo que se envía.

    `nit` y `dv` no se editan: el NIT identifica al emisor (lo usan los documentos y el
    historial del RAG) y el DV se deriva de él.
    """

    name: Optional[str] = Field(
        None, max_length=255, description="Razón social. Única entre emisores.", examples=["Proveedor SAS"]
    )
    phone: Optional[str] = Field(
        None, max_length=100, description="Teléfono. `null` o `\"\"` lo borra.", examples=["3001234567"]
    )
    email: Optional[str] = Field(
        None, max_length=255, description="Correo. Único entre emisores.", examples=["ventas@proveedor.co"]
    )
    account_number: Optional[str] = Field(
        None,
        description=(
            "Cuenta contable de cuentas por pagar: solo dígitos, hasta 20 (el ancho de un código "
            "PUC). `null` o `\"\"` la borra."
        ),
        examples=["220505"],
    )
    tipo_contribuyente: Optional[str] = Field(
        None,
        description=(
            "Códigos de responsabilidad fiscal de la DIAN separados por `;`, no texto libre: "
            "el llm-service los expande a su significado (O-13 Gran contribuyente, O-15 "
            "Autorretenedor, O-23 Agente de retención de IVA…) para decidir qué retenciones "
            "proceden. Un código mal escrito llega al modelo sin significado. `null` o `\"\"` "
            "lo borra."
        ),
        examples=["O-13;O-15;O-48"],
    )
    payment_id: Optional[int] = Field(
        None,
        description=(
            "Medio de pago por defecto (`integration_payment_types.id`). Debe existir y estar "
            "activo. `null` lo desasigna. Solo afecta a los documentos que se procesen después: "
            "los ya cargados conservan el suyo (se cambia por `PATCH /documents/{id}/payment-type`)."
        ),
        examples=[3],
    )
    notes: Optional[str] = Field(
        None,
        description=(
            f"Reglas del contador sobre este proveedor, hasta {NOTES_MAX_LENGTH} caracteres. "
            "**Qué hace el LLM con ellas.**\n"
            "- Hoy solo las lee la sugerencia de retenciones (RF-02): entran al prompt como "
            "`documento.emisor.notas`, junto a razón social, NIT, `tipo_contribuyente` y sus "
            "responsabilidades expandidas.\n"
            f"- Se leen únicamente los primeros {NOTES_MAX_LENGTH} caracteres, con saltos de "
            "línea y caracteres de control convertidos en espacios; por eso aquí se guardan ya "
            "normalizadas.\n"
            "- Son orientativas: no reemplazan tarifas, perfil fiscal ni catálogo de impuestos, y "
            "la validación determinística descarta cualquier sugerencia cuyo porcentaje o base "
            "no cuadre con la tabla oficial, aunque la nota la pida.\n"
            "- Escribir hechos y criterios concretos («presta servicios de transporte; el "
            "contador no retiene ICA en Cali»), no instrucciones al modelo ni cifras de tarifas.\n"
            "- La asignación de cuentas PUC por ítem no las usa.\n"
            "`null` o `\"\"` las borra."
        ),
        examples=["Alquila equipos; el contador retiene en la fuente como arrendamiento."],
    )

    model_config = {
        "json_schema_extra": {
            "example": {
                "account_number": "220505",
                "tipo_contribuyente": "O-13;O-48",
                "payment_id": 3,
                "notes": "Alquila equipos; el contador retiene en la fuente como arrendamiento.",
            }
        }
    }

    @field_validator("name", "email", mode="before")
    @classmethod
    def _required_not_blank(cls, v, info):
        # Columnas NOT NULL: se pueden cambiar, no vaciar.
        if v is None:
            raise ValueError(f"`{info.field_name}` no puede ser nulo.")
        v = str(v).strip()
        if not v:
            raise ValueError(f"`{info.field_name}` no puede estar vacío.")
        return v

    @field_validator("email")
    @classmethod
    def _email_shape(cls, v):
        if not _EMAIL.match(v):
            raise ValueError("Correo con formato inválido.")
        return v.lower()

    @field_validator("phone", mode="before")
    @classmethod
    def _phone(cls, v):
        v = None if v is None else str(v).strip()
        return v or None

    @field_validator("account_number", mode="before")
    @classmethod
    def _account(cls, v):
        v = None if v is None else str(v).strip()
        if not v:
            return None
        if not v.isdigit() or len(v) > 20:
            raise ValueError("Debe contener solo dígitos, hasta 20.")
        return v

    @field_validator("tipo_contribuyente", mode="before")
    @classmethod
    def _responsibilities(cls, v):
        if v is None or not str(v).strip():
            return None
        codes: list[str] = []
        for part in str(v).split(";"):
            code = part.strip().upper()
            if not code:
                continue
            if not _RESPONSIBILITY_CODE.match(code):
                raise ValueError(
                    f"Código de responsabilidad inválido: «{part.strip()}». "
                    "Use códigos DIAN separados por «;» (p. ej. O-13;O-48)."
                )
            if code not in codes:
                codes.append(code)
        return ";".join(codes) or None

    @field_validator("notes", mode="before")
    @classmethod
    def _notes(cls, v):
        if v is None:
            return None
        # Misma normalización que aplica el llm-service antes de incrustar el texto.
        v = _WHITESPACE.sub(" ", _CONTROL_CHARS.sub(" ", str(v))).strip()
        if len(v) > NOTES_MAX_LENGTH:
            raise ValueError(
                f"Máximo {NOTES_MAX_LENGTH} caracteres: el modelo no lee más allá de ese tope."
            )
        return v or None


class IssuerListResponse(BaseModel):
    items: list[IssuerResponse] = Field(description="Emisores de la página solicitada.")
    total: int = Field(description="Total de emisores que cumplen el filtro.", examples=[120])
    limit: int = Field(description="Tamaño de página aplicado.", examples=[50])
    offset: int = Field(description="Desplazamiento aplicado.", examples=[0])
