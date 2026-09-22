from pathlib import Path

from app.domain.ports.storage import DocumentStoragePort
from app.infrastructure.clients.integration_config_client import IntegrationConfigClient
from app.infrastructure.storage.local_storage_adapter import LocalStorageAdapter
from app.infrastructure.storage.s3_storage_adapter import S3StorageAdapter
from app.infrastructure.storage.sharepoint_storage_adapter import SharePointStorageAdapter

_DEFAULT_NAMING_PATTERN = (
    "IKB - DOCU - {fecha_emision} - V01 - {tipo_documento} {numero_documento} {nombre_emisor}"
)


class StorageResolution:
    def __init__(self, backends: list[DocumentStoragePort], naming_pattern: str):
        self.backends = backends
        self.naming_pattern = naming_pattern


async def resolve_storage(
    client: IntegrationConfigClient, tenant_slug: str, local_fallback_dir: Path
) -> StorageResolution:
    """Determina en que backend(s) guardar los artefactos finales (PDF/XML):
    - Si hay S3 y/o SharePoint configurados y activos, se usan esos (uno o ambos), nunca local.
    - Si no hay ninguno configurado (o el servicio no responde), se usa el directorio local.
    """
    settings = await client.get_storage_settings(tenant_slug)
    settings = settings or {}

    backends: list[DocumentStoragePort] = []
    s3_settings = settings.get("s3")
    if s3_settings:
        backends.append(
            S3StorageAdapter(
                bucket_name=s3_settings["bucket_name"],
                region=s3_settings["region"],
                access_key_id=s3_settings["access_key_id"],
                secret_access_key=s3_settings["secret_access_key"],
                endpoint_url=s3_settings.get("endpoint_url"),
                key_prefix=s3_settings.get("key_prefix", ""),
            )
        )

    sharepoint_settings = settings.get("sharepoint")
    if sharepoint_settings:
        backends.append(
            SharePointStorageAdapter(
                azure_tenant_id=sharepoint_settings["azure_tenant_id"],
                client_id=sharepoint_settings["client_id"],
                client_secret=sharepoint_settings["client_secret"],
                site_id=sharepoint_settings["site_id"],
                drive_id=sharepoint_settings["drive_id"],
                folder_path=sharepoint_settings.get("folder_path", ""),
            )
        )

    if not backends:
        backends = [LocalStorageAdapter(local_fallback_dir)]

    naming_pattern = settings.get("naming_pattern") or _DEFAULT_NAMING_PATTERN
    return StorageResolution(backends=backends, naming_pattern=naming_pattern)
