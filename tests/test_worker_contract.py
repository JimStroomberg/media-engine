from __future__ import annotations

import asyncio

import pytest
from fastapi import HTTPException

from app.api.internal import require_worker_auth
from app.config import Settings
from app.services.workers import capabilities_satisfy
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


def test_internal_worker_api_fails_closed_without_token() -> None:
    with pytest.raises(HTTPException) as raised:
        require_worker_auth(authorization=None, settings=Settings(worker_api_token=None))
    assert raised.value.status_code == 503


def test_internal_worker_api_uses_exact_bearer_token() -> None:
    settings = Settings(worker_api_token="secret-token")

    require_worker_auth(authorization="Bearer secret-token", settings=settings)
    with pytest.raises(HTTPException) as raised:
        require_worker_auth(authorization="Bearer wrong-token", settings=settings)
    assert raised.value.status_code == 401


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


@pytest.mark.asyncio
async def test_worker_shutdown_wakes_an_idle_poll_immediately() -> None:
    worker = object.__new__(MediaWorker)
    worker.settings = Settings(worker_poll_seconds=60)
    worker._shutdown = asyncio.Event()

    worker.request_shutdown()

    await asyncio.wait_for(worker._wait_for_poll(), timeout=0.1)
    assert worker._shutdown.is_set()
