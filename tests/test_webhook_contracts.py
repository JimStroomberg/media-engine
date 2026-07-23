from __future__ import annotations

import hashlib
import hmac
import json
import socket
import uuid
from types import SimpleNamespace

import httpx
import pytest

from app.config import Settings
from app.services.webhooks import (
    ClaimedWebhook,
    WebhookDestinationRejected,
    WebhookDispatcher,
    serialize_webhook_payload,
    validate_webhook_destination,
    webhook_signature,
)


def test_webhook_signature_covers_timestamp_and_exact_body() -> None:
    payload = {"event_type": "job.completed", "data": {"job_id": "abc"}}
    body = serialize_webhook_payload(payload)
    expected = hmac.new(
        b"whsec_test",
        b"1720000000." + body,
        hashlib.sha256,
    ).hexdigest()

    assert body == json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    assert webhook_signature("whsec_test", 1720000000, body) == f"v1={expected}"


@pytest.mark.asyncio
async def test_webhook_destination_rejects_private_addresses(monkeypatch) -> None:
    monkeypatch.setattr(
        socket,
        "getaddrinfo",
        lambda *args, **kwargs: [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("127.0.0.1", 443))],
    )

    with pytest.raises(WebhookDestinationRejected, match="non-public"):
        await validate_webhook_destination("https://hooks.example.test/path", allow_private=False)


@pytest.mark.asyncio
async def test_webhook_destination_allows_http_only_in_local_development(monkeypatch) -> None:
    monkeypatch.setattr(
        socket,
        "getaddrinfo",
        lambda *args, **kwargs: [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("127.0.0.1", 8081))],
    )

    await validate_webhook_destination("http://localhost:8081/hook", allow_private=True)
    with pytest.raises(WebhookDestinationRejected, match="HTTPS"):
        await validate_webhook_destination("http://localhost:8081/hook", allow_private=False)


@pytest.mark.asyncio
async def test_dispatcher_posts_signed_json_and_accepts_2xx() -> None:
    secret = "whsec_delivery_test"
    received: dict[str, object] = {}

    async def handler(request: httpx.Request) -> httpx.Response:
        body = await request.aread()
        timestamp = int(request.headers["X-Media-Engine-Timestamp"])
        received["body"] = body
        received["signature"] = request.headers["X-Media-Engine-Signature"]
        received["expected"] = webhook_signature(secret, timestamp, body)
        return httpx.Response(204)

    settings = Settings(_env_file=None)
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(handler),
        follow_redirects=False,
    ) as client:
        dispatcher = WebhookDispatcher(
            settings,
            SimpleNamespace(),
            SimpleNamespace(),
            client,
        )
        claim = ClaimedWebhook(
            event_id=uuid.uuid4(),
            endpoint_id=uuid.uuid4(),
            attempt_number=1,
            lease_token=uuid.uuid4(),
            url="https://receiver.example/hook",
            signing_secret=secret,
            payload={"api_version": "2", "event_type": "job.completed", "data": {"job_id": "abc"}},
        )
        result = await dispatcher._deliver(claim)

    assert result.outcome == "delivered"
    assert result.response_status == 204
    assert received["signature"] == received["expected"]
    assert received["body"] == serialize_webhook_payload(claim.payload)


@pytest.mark.asyncio
async def test_dispatcher_retries_server_errors_but_not_redirects() -> None:
    responses = iter([httpx.Response(503, headers={"Retry-After": "42"}), httpx.Response(302)])

    async def handler(request: httpx.Request) -> httpx.Response:
        return next(responses)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler), follow_redirects=False) as client:
        dispatcher = WebhookDispatcher(
            Settings(_env_file=None),
            SimpleNamespace(),
            SimpleNamespace(),
            client,
        )
        claim = ClaimedWebhook(
            event_id=uuid.uuid4(),
            endpoint_id=uuid.uuid4(),
            attempt_number=1,
            lease_token=uuid.uuid4(),
            url="https://receiver.example/hook",
            signing_secret="whsec_test",
            payload={"event_type": "job.failed"},
        )
        retry = await dispatcher._deliver(claim)
        redirect = await dispatcher._deliver(claim)

    assert retry.outcome == "retryable_http"
    assert retry.retryable
    assert retry.retry_after_seconds == 42
    assert redirect.outcome == "permanent_http"
    assert not redirect.retryable
