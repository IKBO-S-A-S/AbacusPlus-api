from typing import Optional

from pydantic import BaseModel, Field


class S3StorageUpsertRequest(BaseModel):
    bucket_name: str = Field(
        ..., description="Nombre del bucket de S3 destino.", examples=["abacus-documentos-dian"]
    )
    region: str = Field(..., description="Region de AWS del bucket.", examples=["us-east-1"])
    access_key_id: str = Field(
        ..., description="Access Key ID de AWS.", examples=["AKIAIOSFODNN7EXAMPLE"]
    )
    secret_access_key: str = Field(
        ...,
        description="Secret Access Key de AWS. Se cifra antes de persistir y nunca se retorna.",
        examples=["wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY"],
    )
    endpoint_url: Optional[str] = Field(
        None,
        description="Endpoint personalizado para S3 compatible (ej. MinIO). Vacio usa AWS S3.",
        examples=["https://s3.us-east-1.amazonaws.com"],
    )
    key_prefix: str = Field(
        "",
        description="Prefijo/carpeta dentro del bucket donde se guardaran los documentos.",
        examples=["dian/facturas"],
    )

    model_config = {
        "json_schema_extra": {
            "example": {
                "bucket_name": "abacus-documentos-dian",
                "region": "us-east-1",
                "access_key_id": "AKIAIOSFODNN7EXAMPLE",
                "secret_access_key": "wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY",
                "endpoint_url": None,
                "key_prefix": "dian/facturas",
            }
        }
    }


class S3StorageResponse(BaseModel):
    id: int = Field(..., description="ID de la configuracion.", examples=[1])
    bucket_name: str = Field(..., description="Nombre del bucket configurado.")
    region: str = Field(..., description="Region de AWS del bucket.")
    access_key_id: str = Field(..., description="Access Key ID configurado.")
    endpoint_url: Optional[str] = Field(None, description="Endpoint personalizado, si aplica.")
    key_prefix: str = Field(..., description="Prefijo/carpeta configurado dentro del bucket.")
    active: bool = Field(..., description="Indica si la configuracion esta activa.")

    model_config = {"from_attributes": True}


class SharePointStorageUpsertRequest(BaseModel):
    azure_tenant_id: str = Field(
        ...,
        description="Tenant ID de Azure Active Directory donde esta registrada la app.",
        examples=["11111111-2222-3333-4444-555555555555"],
    )
    client_id: str = Field(
        ...,
        description="Client ID (Application ID) de la app registrada en Azure AD.",
        examples=["66666666-7777-8888-9999-000000000000"],
    )
    client_secret: str = Field(
        ...,
        description="Client secret de la app. Se cifra antes de persistir y nunca se retorna.",
        examples=["abcDEF123~ejemploDeSecreto"],
    )
    site_id: str = Field(
        ...,
        description="ID del sitio de SharePoint (Microsoft Graph) donde se guardaran los documentos.",
        examples=["contoso.sharepoint.com,11111111-...,22222222-..."],
    )
    drive_id: str = Field(
        ...,
        description="ID de la biblioteca de documentos (drive) dentro del sitio.",
        examples=["b!AbCdEf..."],
    )
    folder_path: str = Field(
        "",
        description="Ruta de carpeta dentro de la biblioteca donde se guardaran los documentos.",
        examples=["Facturas/DIAN"],
    )

    model_config = {
        "json_schema_extra": {
            "example": {
                "azure_tenant_id": "11111111-2222-3333-4444-555555555555",
                "client_id": "66666666-7777-8888-9999-000000000000",
                "client_secret": "abcDEF123~ejemploDeSecreto",
                "site_id": "contoso.sharepoint.com,11111111-...,22222222-...",
                "drive_id": "b!AbCdEf...",
                "folder_path": "Facturas/DIAN",
            }
        }
    }


class SharePointStorageResponse(BaseModel):
    id: int = Field(..., description="ID de la configuracion.", examples=[1])
    azure_tenant_id: str = Field(..., description="Tenant ID de Azure AD configurado.")
    client_id: str = Field(..., description="Client ID de la app registrada.")
    site_id: str = Field(..., description="ID del sitio de SharePoint configurado.")
    drive_id: str = Field(..., description="ID de la biblioteca de documentos configurada.")
    folder_path: str = Field(..., description="Ruta de carpeta configurada.")
    active: bool = Field(..., description="Indica si la configuracion esta activa.")

    model_config = {"from_attributes": True}


class DocumentNamingUpsertRequest(BaseModel):
    filename_pattern: str = Field(
        ...,
        description=(
            "Patron de nombre de archivo aplicado tanto al PDF como al XML (misma base, "
            "distinta extension). Placeholders soportados: {nit_emisor}, {nombre_emisor}, "
            "{nit_receptor}, {nombre_receptor}, {tipo_documento}, {numero_documento}, "
            "{fecha_emision}, {cufe}."
        ),
        examples=["{nit_emisor} - {numero_documento} - {fecha_emision}"],
    )

    model_config = {
        "json_schema_extra": {
            "example": {"filename_pattern": "{nit_emisor} - {numero_documento} - {fecha_emision}"}
        }
    }


class DocumentNamingResponse(BaseModel):
    id: int = Field(..., description="ID de la configuracion.", examples=[1])
    filename_pattern: str = Field(..., description="Patron de nombre configurado.")

    model_config = {"from_attributes": True}


class StorageSettingsResponse(BaseModel):
    """Agregado interno (servicio-a-servicio) con secretos desencriptados."""

    s3: Optional[dict] = Field(
        None, description="Configuracion de S3 con secretos desencriptados, o null si no existe."
    )
    sharepoint: Optional[dict] = Field(
        None,
        description="Configuracion de SharePoint con secretos desencriptados, o null si no existe.",
    )
    naming_pattern: str = Field(
        ..., description="Patron de nombre de archivo vigente (configurado o default)."
    )
