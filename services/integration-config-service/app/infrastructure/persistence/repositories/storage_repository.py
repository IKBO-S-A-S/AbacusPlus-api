from typing import Optional

from sqlalchemy.orm import Session

from app.infrastructure.config.encryption import decrypt, encrypt
from app.infrastructure.persistence.models.storage import (
    DocumentNamingSetting,
    S3StorageConfig,
    SharePointStorageConfig,
)

_SINGLETON_ID = 1


class S3StorageConfigRepository:
    def __init__(self, db: Session):
        self.db = db

    def get(self) -> Optional[S3StorageConfig]:
        return self.db.get(S3StorageConfig, _SINGLETON_ID)

    def upsert(
        self,
        bucket_name: str,
        region: str,
        access_key_id: str,
        secret_access_key: str,
        endpoint_url: Optional[str],
        key_prefix: str,
    ) -> S3StorageConfig:
        config = self.get()
        if config is None:
            config = S3StorageConfig(id=_SINGLETON_ID)
            self.db.add(config)

        config.bucket_name = bucket_name
        config.region = region
        config.access_key_id = access_key_id
        config.secret_access_key_encrypted = encrypt(secret_access_key)
        config.endpoint_url = endpoint_url
        config.key_prefix = key_prefix
        config.active = True
        self.db.commit()
        self.db.refresh(config)
        return config

    @staticmethod
    def decrypt_secret(config: S3StorageConfig) -> str:
        return decrypt(config.secret_access_key_encrypted)


class SharePointStorageConfigRepository:
    def __init__(self, db: Session):
        self.db = db

    def get(self) -> Optional[SharePointStorageConfig]:
        return self.db.get(SharePointStorageConfig, _SINGLETON_ID)

    def upsert(
        self,
        azure_tenant_id: str,
        client_id: str,
        client_secret: str,
        site_id: str,
        drive_id: str,
        folder_path: str,
    ) -> SharePointStorageConfig:
        config = self.get()
        if config is None:
            config = SharePointStorageConfig(id=_SINGLETON_ID)
            self.db.add(config)

        config.azure_tenant_id = azure_tenant_id
        config.client_id = client_id
        config.client_secret_encrypted = encrypt(client_secret)
        config.site_id = site_id
        config.drive_id = drive_id
        config.folder_path = folder_path
        config.active = True
        self.db.commit()
        self.db.refresh(config)
        return config

    @staticmethod
    def decrypt_secret(config: SharePointStorageConfig) -> str:
        return decrypt(config.client_secret_encrypted)


class DocumentNamingSettingRepository:
    def __init__(self, db: Session):
        self.db = db

    def get(self) -> Optional[DocumentNamingSetting]:
        return self.db.get(DocumentNamingSetting, _SINGLETON_ID)

    def upsert(self, filename_pattern: str) -> DocumentNamingSetting:
        setting = self.get()
        if setting is None:
            setting = DocumentNamingSetting(id=_SINGLETON_ID)
            self.db.add(setting)

        setting.filename_pattern = filename_pattern
        self.db.commit()
        self.db.refresh(setting)
        return setting
