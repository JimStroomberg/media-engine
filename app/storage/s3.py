from __future__ import annotations

import asyncio
from dataclasses import dataclass
from pathlib import Path

import boto3
from botocore.client import Config
from botocore.exceptions import ClientError

from ..config import Settings


@dataclass(frozen=True)
class StoredObject:
    bucket: str
    key: str
    etag: str | None


class S3Store:
    """Small asynchronous facade around the blocking boto3 S3 client."""

    def __init__(self, settings: Settings) -> None:
        if not settings.s3_bucket:
            raise ValueError("MEDIA_ENGINE_S3_BUCKET is required")
        self.settings = settings
        self.client = self._create_client(settings.s3_endpoint_url)
        public_endpoint = settings.s3_public_endpoint_url or settings.s3_endpoint_url
        self.public_client = (
            self.client if public_endpoint == settings.s3_endpoint_url else self._create_client(public_endpoint)
        )
        self.bucket = settings.s3_bucket
        self.region = settings.s3_region
        self.custom_endpoint = bool(settings.s3_endpoint_url)

    def _create_client(self, endpoint_url: str | None):
        client_options: dict[str, object] = {
            "service_name": "s3",
            "region_name": self.settings.s3_region,
            "config": Config(
                s3={"addressing_style": "path" if self.settings.s3_force_path_style else "auto"},
                signature_version="s3v4",
            ),
        }
        if endpoint_url:
            client_options["endpoint_url"] = endpoint_url
        if self.settings.s3_access_key_id:
            client_options["aws_access_key_id"] = self.settings.s3_access_key_id
        if self.settings.s3_secret_access_key:
            client_options["aws_secret_access_key"] = self.settings.s3_secret_access_key
        return boto3.client(**client_options)

    async def check_bucket(self) -> None:
        await asyncio.to_thread(self.client.head_bucket, Bucket=self.bucket)

    async def ensure_bucket(self) -> None:
        """Create the configured bucket when it is absent."""

        try:
            await self.check_bucket()
            return
        except ClientError as exc:
            code = str(exc.response.get("Error", {}).get("Code", ""))
            if code not in {"400", "404", "NoSuchBucket", "NotFound"}:
                raise
        create_options: dict[str, object] = {"Bucket": self.bucket}
        if not self.custom_endpoint and self.region != "us-east-1":
            create_options["CreateBucketConfiguration"] = {"LocationConstraint": self.region}
        await asyncio.to_thread(self.client.create_bucket, **create_options)
        await self.check_bucket()

    async def exists(self, key: str) -> bool:
        try:
            await asyncio.to_thread(self.client.head_object, Bucket=self.bucket, Key=key)
            return True
        except ClientError as exc:
            code = str(exc.response.get("Error", {}).get("Code", ""))
            if code in {"404", "NoSuchKey", "NotFound"}:
                return False
            raise

    async def upload_file(
        self,
        path: Path,
        key: str,
        *,
        sha256: str,
        media_type: str | None,
    ) -> StoredObject:
        extra_args: dict[str, object] = {"Metadata": {"sha256": sha256}}
        if media_type:
            extra_args["ContentType"] = media_type
        await asyncio.to_thread(
            self.client.upload_file,
            str(path),
            self.bucket,
            key,
            ExtraArgs=extra_args,
        )
        response = await asyncio.to_thread(self.client.head_object, Bucket=self.bucket, Key=key)
        etag = str(response.get("ETag", "")).strip('"') or None
        return StoredObject(bucket=self.bucket, key=key, etag=etag)

    async def download_file(self, key: str, destination: Path) -> None:
        destination.parent.mkdir(parents=True, exist_ok=True)
        await asyncio.to_thread(self.client.download_file, self.bucket, key, str(destination))

    def create_download_url(self, key: str, *, expires_in: int, filename: str | None = None) -> str:
        params = {"Bucket": self.bucket, "Key": key}
        if filename:
            params["ResponseContentDisposition"] = f'attachment; filename="{filename}"'
        return self.public_client.generate_presigned_url(
            "get_object",
            Params=params,
            ExpiresIn=expires_in,
        )

    async def delete(self, key: str) -> None:
        await asyncio.to_thread(self.client.delete_object, Bucket=self.bucket, Key=key)
