from __future__ import annotations

import hashlib
import os
import tempfile
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import aiofiles
from fastapi import UploadFile
from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from ..config import Settings
from ..domain.content import content_addressed_key
from ..persistence.models import Asset, Blob
from ..storage.s3 import S3Store


class AssetUploadTooLarge(RuntimeError):
    pass


@dataclass(frozen=True)
class AssetIngestResult:
    asset: Asset
    duplicate_content: bool


@dataclass(frozen=True)
class StagedUpload:
    path: Path
    sha256: str
    size_bytes: int


class AssetService:
    def __init__(self, settings: Settings, store: S3Store) -> None:
        self.settings = settings
        self.store = store

    async def ingest_upload(
        self,
        session: AsyncSession,
        upload: UploadFile,
        *,
        client_id: uuid.UUID,
        source_metadata: dict[str, Any],
        source_kind: str = "upload",
        source_uri: str | None = None,
    ) -> AssetIngestResult:
        staged = await self._stage_upload(upload)
        now = datetime.now(UTC)
        expires_at = now + timedelta(hours=self.settings.asset_retention_hours)
        object_key = content_addressed_key(staged.sha256)
        original_name = Path(upload.filename or "upload").name
        media_type = upload.content_type

        try:
            # Serialize ingest with retention for an existing content hash. The
            # lock remains held through the S3 decision and database commit so
            # an expiry sweep cannot delete a blob while it is being reused or
            # restored.
            existing = await session.scalar(select(Blob).where(Blob.sha256 == staged.sha256).with_for_update())
            duplicate_content = bool(
                existing
                and existing.state == "available"
                and existing.expires_at > now
                and await self.store.exists(existing.object_key)
            )

            if duplicate_content and existing is not None:
                if existing.expires_at < expires_at:
                    existing.expires_at = expires_at
                blob_id = existing.id
            else:
                stored = await self.store.upload_file(
                    staged.path,
                    object_key,
                    sha256=staged.sha256,
                    media_type=media_type,
                )
                statement = (
                    insert(Blob)
                    .values(
                        id=uuid.uuid4(),
                        sha256=staged.sha256,
                        size_bytes=staged.size_bytes,
                        media_type=media_type,
                        bucket=stored.bucket,
                        object_key=stored.key,
                        etag=stored.etag,
                        state="available",
                        expires_at=expires_at,
                        expired_at=None,
                    )
                    .on_conflict_do_update(
                        index_elements=[Blob.sha256],
                        set_={
                            "size_bytes": staged.size_bytes,
                            "media_type": media_type,
                            "bucket": stored.bucket,
                            "object_key": stored.key,
                            "etag": stored.etag,
                            "state": "available",
                            "expires_at": func.greatest(Blob.expires_at, expires_at),
                            "expired_at": None,
                        },
                    )
                    .returning(Blob.id)
                )
                blob_id = (await session.execute(statement)).scalar_one()

            asset = Asset(
                id=uuid.uuid4(),
                client_id=client_id,
                blob_id=blob_id,
                source_filename=original_name,
                source_kind=source_kind,
                source_uri=source_uri,
                source_metadata=source_metadata,
                expires_at=expires_at,
            )
            session.add(asset)
            await session.commit()
            # A PostgreSQL upsert can update a Blob that is already present in
            # this session's identity map (for example, when expired content is
            # uploaded again). Refresh that row so the response reflects the
            # state committed by the upsert rather than the pre-upsert object.
            if existing is not None:
                await session.refresh(existing)
            loaded = await session.scalar(select(Asset).where(Asset.id == asset.id))
            if loaded is None:
                raise RuntimeError("Asset disappeared after ingest commit")
            return AssetIngestResult(asset=loaded, duplicate_content=duplicate_content)
        except Exception:
            await session.rollback()
            raise
        finally:
            staged.path.unlink(missing_ok=True)

    async def get_asset(
        self,
        session: AsyncSession,
        asset_id: uuid.UUID,
        *,
        client_id: uuid.UUID,
    ) -> Asset | None:
        return await session.scalar(
            select(Asset).where(Asset.id == asset_id, Asset.client_id == client_id)
        )

    async def _stage_upload(self, upload: UploadFile) -> StagedUpload:
        self.settings.temp_dir.mkdir(parents=True, exist_ok=True)
        descriptor, raw_path = tempfile.mkstemp(prefix="asset-", suffix=".upload", dir=self.settings.temp_dir)
        os.close(descriptor)
        path = Path(raw_path)
        digest = hashlib.sha256()
        size_bytes = 0
        try:
            async with aiofiles.open(path, "wb") as destination:
                while chunk := await upload.read(1024 * 1024):
                    size_bytes += len(chunk)
                    if self.settings.max_upload_bytes is not None and size_bytes > self.settings.max_upload_bytes:
                        raise AssetUploadTooLarge(
                            f"Upload exceeds MEDIA_ENGINE_MAX_UPLOAD_BYTES={self.settings.max_upload_bytes}"
                        )
                    digest.update(chunk)
                    await destination.write(chunk)
            if size_bytes == 0:
                raise ValueError("Uploaded media is empty")
            return StagedUpload(path=path, sha256=digest.hexdigest(), size_bytes=size_bytes)
        except Exception:
            path.unlink(missing_ok=True)
            raise
        finally:
            await upload.close()
