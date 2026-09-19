from pathlib import Path
from unittest.mock import AsyncMock

import pytest
from app.application.use_cases.resolve_storage_backends import resolve_storage
from app.infrastructure.storage.local_storage_adapter import LocalStorageAdapter
from app.infrastructure.storage.s3_storage_adapter import S3StorageAdapter
from app.infrastructure.storage.sharepoint_storage_adapter import SharePointStorageAdapter

S3_SETTINGS = {
    "bucket_name": "bucket",
    "region": "us-east-1",
    "access_key_id": "AKIA...",
    "secret_access_key": "secret",
    "endpoint_url": None,
    "key_prefix": "dian",
}

SHAREPOINT_SETTINGS = {
    "azure_tenant_id": "tenant-id",
    "client_id": "client-id",
    "client_secret": "secret",
    "site_id": "site-id",
    "drive_id": "drive-id",
    "folder_path": "Facturas",
}


def _mock_client(settings: dict):
    client = AsyncMock()
    client.get_storage_settings.return_value = settings
    return client


class TestResolveStorage:
    @pytest.mark.asyncio
    async def test_falls_back_to_local_when_nothing_configured(self, tmp_path: Path):
        client = _mock_client({"s3": None, "sharepoint": None, "naming_pattern": "custom"})
        resolution = await resolve_storage(client, "tenant1", tmp_path)
        assert len(resolution.backends) == 1
        assert isinstance(resolution.backends[0], LocalStorageAdapter)
        assert resolution.naming_pattern == "custom"

    @pytest.mark.asyncio
    async def test_falls_back_to_local_when_service_unavailable(self, tmp_path: Path):
        client = _mock_client(None)
        resolution = await resolve_storage(client, "tenant1", tmp_path)
        assert len(resolution.backends) == 1
        assert isinstance(resolution.backends[0], LocalStorageAdapter)

    @pytest.mark.asyncio
    async def test_uses_only_s3_when_only_s3_configured(self, tmp_path: Path):
        client = _mock_client(
            {"s3": S3_SETTINGS, "sharepoint": None, "naming_pattern": "pattern"}
        )
        resolution = await resolve_storage(client, "tenant1", tmp_path)
        assert len(resolution.backends) == 1
        assert isinstance(resolution.backends[0], S3StorageAdapter)

    @pytest.mark.asyncio
    async def test_uses_only_sharepoint_when_only_sharepoint_configured(self, tmp_path: Path):
        client = _mock_client(
            {"s3": None, "sharepoint": SHAREPOINT_SETTINGS, "naming_pattern": "pattern"}
        )
        resolution = await resolve_storage(client, "tenant1", tmp_path)
        assert len(resolution.backends) == 1
        assert isinstance(resolution.backends[0], SharePointStorageAdapter)

    @pytest.mark.asyncio
    async def test_uses_both_when_both_configured_and_skips_local(self, tmp_path: Path):
        client = _mock_client(
            {
                "s3": S3_SETTINGS,
                "sharepoint": SHAREPOINT_SETTINGS,
                "naming_pattern": "pattern",
            }
        )
        resolution = await resolve_storage(client, "tenant1", tmp_path)
        assert len(resolution.backends) == 2
        assert not any(isinstance(b, LocalStorageAdapter) for b in resolution.backends)

    @pytest.mark.asyncio
    async def test_default_naming_pattern_when_missing(self, tmp_path: Path):
        client = _mock_client({"s3": None, "sharepoint": None, "naming_pattern": None})
        resolution = await resolve_storage(client, "tenant1", tmp_path)
        assert "IKB - DOCU" in resolution.naming_pattern
