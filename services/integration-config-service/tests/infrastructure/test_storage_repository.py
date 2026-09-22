from app.infrastructure.persistence.repositories.storage_repository import (
    DocumentNamingSettingRepository,
    S3StorageConfigRepository,
    SharePointStorageConfigRepository,
)


class TestS3StorageConfigRepository:
    def test_upsert_creates_single_row(self, db_session):
        repo = S3StorageConfigRepository(db_session)
        repo.upsert(
            bucket_name="bucket-a",
            region="us-east-1",
            access_key_id="AKIA1",
            secret_access_key="secret1",
            endpoint_url=None,
            key_prefix="dian",
        )
        repo.upsert(
            bucket_name="bucket-b",
            region="us-west-2",
            access_key_id="AKIA2",
            secret_access_key="secret2",
            endpoint_url=None,
            key_prefix="facturas",
        )

        config = repo.get()
        assert config.bucket_name == "bucket-b"
        assert config.region == "us-west-2"

        from app.infrastructure.persistence.models.storage import S3StorageConfig

        assert db_session.query(S3StorageConfig).count() == 1

    def test_secret_is_encrypted_at_rest(self, db_session):
        repo = S3StorageConfigRepository(db_session)
        config = repo.upsert(
            bucket_name="bucket-a",
            region="us-east-1",
            access_key_id="AKIA1",
            secret_access_key="super-secret",
            endpoint_url=None,
            key_prefix="",
        )
        assert config.secret_access_key_encrypted != "super-secret"
        assert repo.decrypt_secret(config) == "super-secret"


class TestSharePointStorageConfigRepository:
    def test_upsert_creates_single_row(self, db_session):
        repo = SharePointStorageConfigRepository(db_session)
        repo.upsert(
            azure_tenant_id="tenant-1",
            client_id="client-1",
            client_secret="secret-1",
            site_id="site-1",
            drive_id="drive-1",
            folder_path="Facturas",
        )
        repo.upsert(
            azure_tenant_id="tenant-2",
            client_id="client-2",
            client_secret="secret-2",
            site_id="site-2",
            drive_id="drive-2",
            folder_path="DIAN",
        )

        from app.infrastructure.persistence.models.storage import SharePointStorageConfig

        assert db_session.query(SharePointStorageConfig).count() == 1
        config = repo.get()
        assert config.site_id == "site-2"
        assert repo.decrypt_secret(config) == "secret-2"


class TestDocumentNamingSettingRepository:
    def test_get_returns_none_when_not_configured(self, db_session):
        repo = DocumentNamingSettingRepository(db_session)
        assert repo.get() is None

    def test_upsert_creates_single_row(self, db_session):
        repo = DocumentNamingSettingRepository(db_session)
        repo.upsert("{nit_emisor}-{numero_documento}")
        repo.upsert("{numero_documento}")

        from app.infrastructure.persistence.models.storage import DocumentNamingSetting

        assert db_session.query(DocumentNamingSetting).count() == 1
        assert repo.get().filename_pattern == "{numero_documento}"
