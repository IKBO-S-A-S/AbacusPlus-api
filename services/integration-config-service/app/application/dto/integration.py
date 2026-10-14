from datetime import datetime
from typing import Any, Optional

from pydantic import BaseModel, Field, HttpUrl

from app.application.dto.connection import ConnectionCheckResponse


class CredentialUpsertRequest(BaseModel):
    provider: str = Field(..., description="Proveedor de integracion.", examples=["siigo"])
    account_key: str = Field(
        "default",
        description="Llave interna de la empresa/cuenta conectada.",
        examples=["empresa-principal"],
    )
    username: str = Field(
        ...,
        description="Usuario o identificador de autenticacion del proveedor.",
        examples=["api@empresa.com"],
    )
    access_key: str = Field(
        ...,
        description="Clave/API key/token base entregado por el proveedor.",
        examples=["1234567890"],
    )
    base_url: HttpUrl = Field(
        ..., description="URL base del API externo.", examples=["https://api.siigo.com"]
    )
    partner_id: Optional[str] = Field(
        None,
        description="Identificador de partner/aplicacion si el proveedor lo requiere.",
        examples=["abacusplus"],
    )
    auth_scheme: str = Field(
        "oauth_jwt",
        description="Esquema de autenticacion de la integracion.",
        examples=["oauth_jwt"],
    )
    extra_config: dict[str, Any] = Field(
        default_factory=dict,
        description="Configuracion adicional del proveedor, como rutas de catalogos o flags.",
        examples=[{"chart_accounts_path": "/v1/accounts"}],
    )

    model_config = {
        "json_schema_extra": {
            "example": {
                "provider": "siigo",
                "account_key": "empresa-principal",
                "username": "api@empresa.com",
                "access_key": "1234567890",
                "base_url": "https://api.siigo.com",
                "partner_id": "abacusplus",
                "auth_scheme": "oauth_jwt",
                "extra_config": {"chart_accounts_path": "/v1/accounts"},
            }
        }
    }


class CredentialResponse(BaseModel):
    id: int = Field(..., description="ID local de la credencial.", examples=[1])
    provider: str = Field(..., description="Proveedor de integracion.", examples=["siigo"])
    account_key: str = Field(
        ...,
        description="Llave interna de la empresa/cuenta conectada.",
        examples=["empresa-principal"],
    )
    username: Optional[str] = Field(
        None, description="Usuario registrado.", examples=["api@empresa.com"]
    )
    base_url: str = Field(
        ..., description="URL base usada por el conector.", examples=["https://api.siigo.com"]
    )
    partner_id: Optional[str] = Field(
        None, description="Partner/aplicacion registrada.", examples=["abacusplus"]
    )
    auth_scheme: str = Field(
        ..., description="Esquema de autenticacion configurado.", examples=["oauth_jwt"]
    )
    expires_at: Optional[datetime] = Field(
        None, description="Fecha de expiracion del token actual si aplica."
    )
    extra_config: dict[str, Any] = Field(..., description="Configuracion adicional no sensible.")
    active: bool = Field(..., description="Indica si la credencial esta activa.", examples=[True])
    connection: Optional[ConnectionCheckResponse] = Field(
        None,
        description=(
            "Resultado de validar la conexion al guardar. Solo viene en la respuesta del PUT y "
            "solo para proveedores que se pueden validar (hoy SIIGO); un fallo no impide que "
            "la credencial quede guardada."
        ),
    )

    model_config = {"from_attributes": True}
