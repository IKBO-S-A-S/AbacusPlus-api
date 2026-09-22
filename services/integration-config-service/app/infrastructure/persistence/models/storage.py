from sqlalchemy import Boolean, Column, DateTime, Integer, String, Text, func

from app.infrastructure.config.database import Base


class S3StorageConfig(Base):
    __tablename__ = "s3_storage_configs"

    id = Column(Integer, primary_key=True, default=1)
    bucket_name = Column(String(255), nullable=False)
    region = Column(String(50), nullable=False)
    access_key_id = Column(Text, nullable=False)
    secret_access_key_encrypted = Column(Text, nullable=False)
    endpoint_url = Column(String(255), nullable=True)
    key_prefix = Column(String(255), nullable=True, default="")
    active = Column(Boolean, nullable=False, default=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    updated_at = Column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )


class SharePointStorageConfig(Base):
    __tablename__ = "sharepoint_storage_configs"

    id = Column(Integer, primary_key=True, default=1)
    azure_tenant_id = Column(String(255), nullable=False)
    client_id = Column(String(255), nullable=False)
    client_secret_encrypted = Column(Text, nullable=False)
    site_id = Column(String(255), nullable=False)
    drive_id = Column(String(255), nullable=False)
    folder_path = Column(String(500), nullable=True, default="")
    active = Column(Boolean, nullable=False, default=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    updated_at = Column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )


class DocumentNamingSetting(Base):
    __tablename__ = "document_naming_settings"

    id = Column(Integer, primary_key=True, default=1)
    filename_pattern = Column(
        String(500),
        nullable=False,
        default=(
            "IKB - DOCU - {fecha_emision} - V01 - {tipo_documento} "
            "{numero_documento} {nombre_emisor}"
        ),
    )
    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    updated_at = Column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )
