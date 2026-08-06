from __future__ import annotations

import asyncio
import uuid
from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock

import httpx
import pytest

from app.api.internal import (
    ArtifactCompletionRequest,
    ArtifactUploadPrepareResponse,
    StageHeartbeatRequest,
    StageSourceResponse,
)
from app.config import Settings
from app.persistence.models import Worker
from app.security import generate_worker_token
from app.services.worker_access import WorkerAccessService, WorkerConflict, WorkerRemovalConflict
from app.services.workers import WorkerService, capabilities_satisfy
from app.storage.s3 import S3Store
from app.worker import MediaWorker


def test_capability_matching_requires_every_value() -> None:
    actual = {
        "pipelines": ["transcode"],
        "backends": ["cpu"],
        "encoders": ["h264", "h265"],
    }

    assert capabilities_satisfy(actual, {"pipelines": ["transcode"]})
    assert capabilities_satisfy(actual, {"encoders": ["h265", "h264"]})
    assert not capabilities_satisfy(actual, {"backends": ["rkmpp"]})
    assert not capabilities_satisfy(actual, {"pipelines": "transcode"})


@pytest.mark.asyncio
async def test_worker_token_authenticates_exactly_one_worker() -> None:
    generated = generate_worker_token()
    worker = Worker(
        id=uuid.uuid4(),
        worker_key="worker-test",
        display_name="Test worker",
        profile="cpu",
        capabilities={},
        runtime={},
        status="offline",
        desired_state="active",
        credential_prefix=generated.prefix,
        credential_hash=generated.token_hash,
        credential_created_at=datetime.now(UTC),
        credential_expires_at=None,
        credential_revoked_at=None,
        created_at=datetime.now(UTC),
        registered_at=None,
        last_seen_at=None,
    )
    session = AsyncMock()
    session.scalar.return_value = worker

    principal = await WorkerAccessService().authenticate(session, generated.token)

    assert principal is not None
    assert principal.worker_id == worker.id
    assert worker.credential_last_used_at is not None
    session.commit.assert_awaited_once()


@pytest.mark.asyncio
async def test_worker_token_rejects_wrong_revoked_and_expired_credentials() -> None:
    generated = generate_worker_token()
    worker = Worker(
        id=uuid.uuid4(),
        worker_key="worker-test",
        display_name="Test worker",
        profile="cpu",
        capabilities={},
        runtime={},
        status="offline",
        desired_state="active",
        credential_prefix=generated.prefix,
        credential_hash=generated.token_hash,
        credential_created_at=datetime.now(UTC),
        credential_expires_at=None,
        credential_revoked_at=None,
        created_at=datetime.now(UTC),
        registered_at=None,
        last_seen_at=None,
    )
    session = AsyncMock()
    session.scalar.return_value = worker
    service = WorkerAccessService()

    assert await service.authenticate(session, f"mew_{generated.prefix}_wrong") is None
    worker.credential_revoked_at = datetime.now(UTC)
    assert await service.authenticate(session, generated.token) is None
    worker.credential_revoked_at = None
    worker.credential_expires_at = datetime.now(UTC) - timedelta(seconds=1)
    assert await service.authenticate(session, generated.token) is None
    worker.credential_expires_at = None
    worker.removed_at = datetime.now(UTC)
    assert await service.authenticate(session, generated.token) is None


@pytest.mark.asyncio
async def test_only_revoked_idle_workers_can_be_removed() -> None:
    generated = generate_worker_token()
    worker = Worker(
        id=uuid.uuid4(),
        worker_key="worker-retired",
        display_name="Retired worker",
        profile="cpu",
        capabilities={},
        runtime={},
        status="offline",
        desired_state="revoked",
        credential_prefix=generated.prefix,
        credential_hash=generated.token_hash,
        credential_created_at=datetime.now(UTC),
        credential_expires_at=None,
        credential_revoked_at=datetime.now(UTC),
        created_at=datetime.now(UTC),
        registered_at=datetime.now(UTC),
        last_seen_at=datetime.now(UTC),
    )
    session = AsyncMock()
    session.scalar.return_value = 1

    with pytest.raises(WorkerRemovalConflict, match="active lease"):
        await WorkerAccessService().remove(session, worker)

    session.scalar.return_value = 0

    await WorkerAccessService().remove(session, worker)

    assert worker.removed_at is not None
    assert worker.credential_prefix is None
    assert worker.credential_hash is None
    session.commit.assert_awaited_once()

    worker.removed_at = None
    worker.desired_state = "active"
    with pytest.raises(WorkerRemovalConflict, match="Revoke the worker"):
        await WorkerAccessService().remove(session, worker)


@pytest.mark.asyncio
async def test_initial_worker_import_preserves_managed_lifecycle_state() -> None:
    generated = generate_worker_token()
    created_at = datetime.now(UTC) - timedelta(days=1)
    last_used_at = datetime.now(UTC) - timedelta(minutes=1)
    worker = Worker(
        id=uuid.uuid4(),
        worker_key="worker-cpu-1",
        display_name="Old name",
        profile="cpu",
        capabilities={},
        runtime={},
        status="online",
        desired_state="draining",
        credential_prefix=generated.prefix,
        credential_hash=generated.token_hash,
        credential_created_at=created_at,
        credential_last_used_at=last_used_at,
        credential_expires_at=None,
        credential_revoked_at=None,
        created_at=created_at,
        registered_at=created_at,
        last_seen_at=last_used_at,
    )
    session = AsyncMock()
    session.scalar.side_effect = [None, worker]

    imported = await WorkerAccessService().import_initial(
        session,
        worker_key="worker-cpu-1",
        display_name="Compose CPU worker",
        profile="cpu",
        token=generated.token,
    )

    assert imported.desired_state == "draining"
    assert imported.credential_created_at == created_at
    assert imported.credential_last_used_at == last_used_at
    session.commit.assert_awaited_once()


@pytest.mark.asyncio
async def test_initial_worker_import_rejects_stale_environment_token() -> None:
    configured = generate_worker_token()
    rotated = generate_worker_token()
    worker = Worker(
        id=uuid.uuid4(),
        worker_key="worker-cpu-1",
        display_name="Compose CPU worker",
        profile="cpu",
        capabilities={},
        runtime={},
        status="offline",
        desired_state="active",
        credential_prefix=rotated.prefix,
        credential_hash=rotated.token_hash,
        credential_created_at=datetime.now(UTC),
        credential_expires_at=None,
        credential_revoked_at=None,
        created_at=datetime.now(UTC),
        registered_at=None,
        last_seen_at=None,
    )
    session = AsyncMock()
    session.scalar.side_effect = [None, worker]

    with pytest.raises(WorkerConflict, match="latest rotated token"):
        await WorkerAccessService().import_initial(
            session,
            worker_key="worker-cpu-1",
            display_name="Compose CPU worker",
            profile="cpu",
            token=configured.token,
        )

    session.commit.assert_not_awaited()


@pytest.mark.asyncio
async def test_draining_worker_cannot_claim_new_work() -> None:
    worker = Worker(
        id=uuid.uuid4(),
        worker_key="worker-test",
        display_name="Test worker",
        profile="cpu",
        capabilities={},
        runtime={},
        status="online",
        desired_state="draining",
        created_at=datetime.now(UTC),
        registered_at=datetime.now(UTC),
        last_seen_at=datetime.now(UTC),
    )
    session = AsyncMock()
    session.scalar.return_value = worker

    claimed = await WorkerService(Settings(_env_file=None), object()).claim(session, worker_id=worker.id)

    assert claimed is None
    session.commit.assert_awaited_once()


def test_worker_protocol_derives_identity_and_hides_storage_coordinates() -> None:
    assert "worker_id" not in StageHeartbeatRequest.model_fields
    assert "worker_id" not in ArtifactCompletionRequest.model_fields
    assert "download_url" in StageSourceResponse.model_fields
    assert "bucket" not in StageSourceResponse.model_fields
    assert "object_key" not in StageSourceResponse.model_fields
    assert "object_key" not in ArtifactUploadPrepareResponse.model_fields


def test_artifact_url_uses_client_reachable_s3_endpoint() -> None:
    settings = Settings(
        s3_bucket="media",
        s3_endpoint_url="http://minio:9000",
        s3_public_endpoint_url="https://downloads.example.test",
        s3_access_key_id="access-key",
        s3_secret_access_key="secret-key",
    )

    url = S3Store(settings).create_download_url("blobs/sha256/aa/value", expires_in=900, filename="output.mp4")

    assert url.startswith("https://downloads.example.test/media/blobs/sha256/aa/value?")
    assert "response-content-disposition=" in url


def test_worker_transfers_use_the_worker_reachable_s3_endpoint() -> None:
    settings = Settings(
        _env_file=None,
        s3_bucket="media",
        s3_endpoint_url="http://minio:9000",
        s3_public_endpoint_url="https://downloads.example.test",
        s3_worker_endpoint_url="https://worker-storage.example.test",
        s3_access_key_id="access-key",
        s3_secret_access_key="secret-key",
    )
    store = S3Store(settings)

    download_url = store.create_worker_download_url("blobs/sha256/aa/value", expires_in=900)
    upload_url, headers = store.create_upload_url(
        "blobs/sha256/aa/value",
        expires_in=900,
        sha256="a" * 64,
        size_bytes=123,
        media_type="video/mp4",
    )

    assert download_url.startswith("https://worker-storage.example.test/media/")
    assert upload_url.startswith("https://worker-storage.example.test/media/")
    assert headers == {
        "Content-Length": "123",
        "x-amz-meta-sha256": "a" * 64,
        "Content-Type": "video/mp4",
    }


def test_worker_starts_without_s3_credentials() -> None:
    generated = generate_worker_token()

    worker = MediaWorker(Settings(_env_file=None, worker_token=generated.token))

    assert worker.worker_token == generated.token


def test_worker_token_can_be_loaded_from_a_secret_file(tmp_path) -> None:
    generated = generate_worker_token()
    token_path = tmp_path / "worker-token"
    token_path.write_text(f"{generated.token}\n", encoding="utf-8")

    settings = Settings(_env_file=None, worker_token_file=token_path)

    assert settings.resolved_worker_token() == generated.token


@pytest.mark.asyncio
async def test_signed_transfer_errors_do_not_expose_presigned_urls(tmp_path) -> None:
    secret_url = "https://storage.example.test/object?X-Amz-Signature=temporary-secret"

    class FailingDownloadClient:
        def stream(self, method: str, url: str):
            raise httpx.ConnectError("connection failed", request=httpx.Request(method, url))

    worker = object.__new__(MediaWorker)
    with pytest.raises(RuntimeError, match="Signed stage download request failed") as download_error:
        await worker._download_and_verify(
            FailingDownloadClient(),
            {"download_url": secret_url, "sha256": "a" * 64, "size_bytes": 1},
            tmp_path / "input.mp4",
        )
    assert "temporary-secret" not in str(download_error.value)
    assert download_error.value.__suppress_context__ is True

    response = httpx.Response(403, request=httpx.Request("PUT", secret_url))
    upload_client = AsyncMock()
    upload_client.put.side_effect = httpx.HTTPStatusError(
        "forbidden",
        request=response.request,
        response=response,
    )
    source = tmp_path / "output.mp4"
    source.write_bytes(b"output")
    with pytest.raises(RuntimeError, match="Signed artifact upload failed with HTTP 403") as upload_error:
        await worker._upload_file(upload_client, secret_url, {}, source)
    assert "temporary-secret" not in str(upload_error.value)
    assert upload_error.value.__suppress_context__ is True


@pytest.mark.asyncio
async def test_worker_shutdown_wakes_an_idle_poll_immediately() -> None:
    worker = object.__new__(MediaWorker)
    worker.settings = Settings(worker_poll_seconds=60)
    worker._shutdown = asyncio.Event()

    worker.request_shutdown()

    await asyncio.wait_for(worker._wait_for_poll(), timeout=0.1)
    assert worker._shutdown.is_set()
