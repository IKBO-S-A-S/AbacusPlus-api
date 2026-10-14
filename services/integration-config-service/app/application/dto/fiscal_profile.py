from typing import Optional

from pydantic import BaseModel, ConfigDict, Field, field_validator


class FiscalProfileResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    nit: Optional[str] = Field(
        None,
        description="NIT de la empresa: solo dígitos, sin puntos, guiones ni dígito de verificación.",
        examples=["900031352"],
    )
    razon_social: Optional[str] = Field(
        None,
        description="Razón social: nombre legal de la empresa.",
        examples=["Agroindustrial Ejemplo S.A.S."],
    )
    agente_retencion_renta: bool = Field(
        ..., description="La empresa es agente de retención en la fuente (renta / ReteFuente)."
    )
    agente_retencion_ica: bool = Field(..., description="La empresa es agente de retención de ICA.")
    agente_retencion_iva: bool = Field(
        ..., description="La empresa es agente de retención de IVA (ReteIVA)."
    )
    autorretenedor_renta: bool = Field(..., description="La empresa es autorretenedora de renta.")
    gran_contribuyente: bool = Field(..., description="La empresa es Gran Contribuyente.")
    responsable_iva: bool = Field(..., description="La empresa es responsable de IVA.")
    regimen: str = Field(..., description="Régimen tributario: 'ordinario' o 'simple' (RST).")
    notas: Optional[str] = Field(None, description="Notas del contador sobre el perfil.")


class FiscalProfileUpsertRequest(BaseModel):
    nit: Optional[str] = Field(
        default=None,
        description=(
            "NIT de la empresa: solo dígitos (1 a 20), sin puntos, guiones ni dígito de "
            "verificación. Acepta texto o número JSON; siempre se guarda y devuelve como texto."
        ),
        pattern=r"^\d{1,20}$",
        examples=["900031352"],
    )
    @field_validator("nit", mode="before")
    @classmethod
    def _nit_numerico_a_texto(cls, value):
        # Los clientes suelen mandar el NIT como número JSON (901031352). Se acepta y se guarda
        # como texto; un float o un bool no son un NIT y siguen fallando la validación.
        if isinstance(value, int) and not isinstance(value, bool):
            return str(value)
        return value

    razon_social: Optional[str] = Field(
        default=None,
        description="Razón social: nombre legal de la empresa (hasta 255 caracteres).",
        max_length=255,
        examples=["Agroindustrial Ejemplo S.A.S."],
    )
    agente_retencion_renta: bool = False
    agente_retencion_ica: bool = False
    agente_retencion_iva: bool = False
    autorretenedor_renta: bool = False
    gran_contribuyente: bool = False
    responsable_iva: bool = False
    regimen: str = Field(
        default="ordinario",
        description="Régimen tributario: 'ordinario' o 'simple'.",
        pattern="^(ordinario|simple)$",
    )
    notas: Optional[str] = Field(default=None, max_length=500)

    model_config = {
        "json_schema_extra": {
            "example": {
                "nit": "900031352",
                "razon_social": "Agroindustrial Ejemplo S.A.S.",
                "agente_retencion_renta": True,
                "agente_retencion_ica": True,
                "agente_retencion_iva": False,
                "autorretenedor_renta": False,
                "gran_contribuyente": True,
                "responsable_iva": True,
                "regimen": "ordinario",
                "notas": "Confirmado con el contador el 2026-08-09.",
            }
        }
    }
