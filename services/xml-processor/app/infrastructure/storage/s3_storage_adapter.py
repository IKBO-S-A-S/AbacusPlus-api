import asyncio
from typing import Optional

import boto3

from app.domain.ports.storage import DocumentStoragePort


class S3StorageAdapter(DocumentStoragePort):
    def __init__(
        self,
        bucket_name: str,
        region: str,
        access_key_id: str,
        secret_access_key: str,
        endpoint_url: Optional[str] = None,
        key_prefix: str = "",
    ):
        self._bucket_name = bucket_name
        self._key_prefix = key_prefix.strip("/")
        self._client = boto3.client(
            "s3",
            region_name=region,
            aws_access_key_id=access_key_id,
            aws_secret_access_key=secret_access_key,
            endpoint_url=endpoint_url or None,
        )

    def _build_key(self, filename: str) -> str:
        return f"{self._key_prefix}/{filename}" if self._key_prefix else filename

    async def save(self, content: bytes, filename: str) -> str:
        key = self._build_key(filename)
        await asyncio.to_thread(
            self._client.put_object, Bucket=self._bucket_name, Key=key, Body=content
        )
        return f"s3://{self._bucket_name}/{key}"
