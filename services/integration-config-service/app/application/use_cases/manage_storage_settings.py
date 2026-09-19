from app.application.dto.storage import (
    DocumentNamingUpsertRequest,
    S3StorageUpsertRequest,
    SharePointStorageUpsertRequest,
)
from app.domain.exceptions.base import ValidationException
from app.infrastructure.persistence.repositories.storage_repository import (
    DocumentNamingSettingRepository,
    S3StorageConfigRepository,
    SharePointStorageConfigRepository,
)

_DEFAULT_NAMING_PATTERN = (
    "IKB - DOCU - {fecha_emision} - V01 - {tipo_documento} {numero_documento} {nombre_emisor}"
)


class ManageStorageSettingsUseCase:
    def __init__(
        self,
        s3_repository: S3StorageConfigRepository,
        sharepoint_repository: SharePointStorageConfigRepository,
        naming_repository: DocumentNamingSettingRepository,
    ):
        self.s3_repository = s3_repository
        self.sharepoint_repository = sharepoint_repository
        self.naming_repository = naming_repository

    def upsert_s3(self, request: S3StorageUpsertRequest):
        if not request.bucket_name.strip():
            raise ValidationException("bucket_name is required")
        return self.s3_repository.upsert(
            bucket_name=request.bucket_name.strip(),
            region=request.region.strip(),
            access_key_id=request.access_key_id.strip(),
            secret_access_key=request.secret_access_key,
            endpoint_url=request.endpoint_url,
            key_prefix=request.key_prefix.strip(),
        )

    def get_s3(self):
        return self.s3_repository.get()

    def upsert_sharepoint(self, request: SharePointStorageUpsertRequest):
        if not request.site_id.strip():
            raise ValidationException("site_id is required")
        return self.sharepoint_repository.upsert(
            azure_tenant_id=request.azure_tenant_id.strip(),
            client_id=request.client_id.strip(),
            client_secret=request.client_secret,
            site_id=request.site_id.strip(),
            drive_id=request.drive_id.strip(),
            folder_path=request.folder_path.strip(),
        )

    def get_sharepoint(self):
        return self.sharepoint_repository.get()

    def upsert_naming_pattern(self, request: DocumentNamingUpsertRequest):
        pattern = request.filename_pattern.strip()
        if not pattern:
            raise ValidationException("filename_pattern is required")
        return self.naming_repository.upsert(pattern)

    def get_naming_pattern(self):
        setting = self.naming_repository.get()
        return setting.filename_pattern if setting else _DEFAULT_NAMING_PATTERN

    def get_settings(self) -> dict:
        s3 = self.s3_repository.get()
        sharepoint = self.sharepoint_repository.get()
        return {
            "s3": (
                {
                    "bucket_name": s3.bucket_name,
                    "region": s3.region,
                    "access_key_id": s3.access_key_id,
                    "secret_access_key": self.s3_repository.decrypt_secret(s3),
                    "endpoint_url": s3.endpoint_url,
                    "key_prefix": s3.key_prefix,
                }
                if s3 and s3.active
                else None
            ),
            "sharepoint": (
                {
                    "azure_tenant_id": sharepoint.azure_tenant_id,
                    "client_id": sharepoint.client_id,
                    "client_secret": self.sharepoint_repository.decrypt_secret(sharepoint),
                    "site_id": sharepoint.site_id,
                    "drive_id": sharepoint.drive_id,
                    "folder_path": sharepoint.folder_path,
                }
                if sharepoint and sharepoint.active
                else None
            ),
            "naming_pattern": self.get_naming_pattern(),
        }
