from datetime import datetime
from typing import Optional

from pydantic import BaseModel, Field


class EnqueueBatchResponse(BaseModel):
    queued: int
    files: list[str]


class ProcessFileRequest(BaseModel):
    filename: str = Field(
        ..., description="Nombre del archivo ZIP en DOWNLOADS_DIR, ej: 'abc123.zip'."
    )
    job_id: str = Field(
        ...,
        description="ID del job ARQ que originó la descarga. Se usa para actualizar el estado en Redis.",
    )
    tenant_slug: str = Field(
        "",
        description="Slug del tenant (ej: 'ikbo'). Determina la DB destino. Vacío = DB por defecto.",
    )


class ProcessingLogResponse(BaseModel):
    id: int
    filename: str
    xml_filename: Optional[str] = None
    status: str
    document_id: Optional[int] = None
    document_number: Optional[str] = None
    error_message: Optional[str] = None
    accounting_status: Optional[str] = None
    accounting_error: Optional[str] = None
    storage_status: Optional[str] = Field(
        None,
        description=(
            "Resultado de publicar el PDF/XML en los backends configurados del tenant "
            "(S3, SharePoint): `ok` si se subió a todos los configurados, `error` si alguno "
            "falló (ver `storage_error`), `null` si no aplicaba (documento sin archivos aún)."
        ),
    )
    storage_error: Optional[str] = Field(
        None,
        description="Detalle del fallo de publicación por backend, p. ej. un error de conexión con S3 o SharePoint.",
    )
    processed_at: datetime

    model_config = {"from_attributes": True}
