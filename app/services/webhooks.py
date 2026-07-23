from __future__ import annotations

import asyncio
import hashlib
import hmac
import ipaddress
import json
import math
import socket
import time
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any
from urllib.parse import urlsplit

import httpx
from sqlalchemy import or_, select, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from ..config import Settings
from ..persistence.models import (
    JobRequest,
    PipelineRun,
    WebhookDeliveryAttempt,
    WebhookEndpoint,
    WebhookEvent,
)
from ..security import CredentialCipher, credential_hint, generate_webhook_signing_secret

JOB_WEBHOOK_EVENT_TYPES = ("job.completed", "job.failed")


class WebhookConflict(RuntimeError):
    pass


class WebhookEndpointNotFound(RuntimeError):
    pass


class WebhookDestinationRejected(RuntimeError):
    pass


@dataclass(frozen=True)
class CreatedWebhookEndpoint:
    endpoint: WebhookEndpoint
    signing_secret: str


@dataclass(frozen=True)
class ClaimedWebhook:
    event_id: uuid.UUID
    endpoint_id: uuid.UUID
    attempt_number: int
    lease_token: uuid.UUID
    url: str
    signing_secret: str
    payload: dict[str, Any]


@dataclass(frozen=True)
class DeliveryResult:
    outcome: str
    retryable: bool
    started_at: datetime
    completed_at: datetime
    duration_ms: int
    response_status: int | None = None
    response_body_preview: str | None = None
    error_message: str | None = None
    retry_after_seconds: int | None = None


def serialize_webhook_payload(payload: dict[str, Any]) -> bytes:
    """Serialize payloads deterministically so consumers can verify the exact bytes."""

    return json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def webhook_signature(secret: str, timestamp: int, body: bytes) -> str:
    signed = f"{timestamp}.".encode("ascii") + body
    digest = hmac.new(secret.encode("utf-8"), signed, hashlib.sha256).hexdigest()
    return f"v1={digest}"


async def validate_webhook_destination(url: str, *, allow_private: bool) -> None:
    """Reject credentials, non-HTTP schemes, and non-public network targets."""

    parsed = urlsplit(url)
    if parsed.scheme not in {"http", "https"}:
        raise WebhookDestinationRejected("Webhook URL must use HTTPS")
    if parsed.scheme != "https" and not allow_private:
        raise WebhookDestinationRejected("Webhook URL must use HTTPS")
    if parsed.username is not None or parsed.password is not None:
        raise WebhookDestinationRejected("Webhook URL must not contain credentials")
    if parsed.hostname is None:
        raise WebhookDestinationRejected("Webhook URL must include a hostname")
    try:
        port = parsed.port or (443 if parsed.scheme == "https" else 80)
    except ValueError as exc:
        raise WebhookDestinationRejected("Webhook URL has an invalid port") from exc

    try:
        addresses = await asyncio.to_thread(
            socket.getaddrinfo,
            parsed.hostname,
            port,
            type=socket.SOCK_STREAM,
        )
    except socket.gaierror as exc:
        raise WebhookDestinationRejected("Webhook hostname could not be resolved") from exc
    if not addresses:
        raise WebhookDestinationRejected("Webhook hostname could not be resolved")
    if allow_private:
        return

    for address in {row[4][0] for row in addresses}:
        ip = ipaddress.ip_address(address)
        if not ip.is_global:
            raise WebhookDestinationRejected(
                "Webhook URL resolves to a private, local, reserved, or otherwise non-public address"
            )


class WebhookEndpointService:
    def __init__(self, settings: Settings, cipher: CredentialCipher) -> None:
        self.settings = settings
        self.cipher = cipher

    async def list(self, session: AsyncSession, client_id: uuid.UUID) -> list[WebhookEndpoint]:
        return list(
            await session.scalars(
                select(WebhookEndpoint)
                .where(WebhookEndpoint.client_id == client_id)
                .order_by(WebhookEndpoint.created_at)
            )
        )

    async def get(
        self,
        session: AsyncSession,
        endpoint_id: uuid.UUID,
        *,
        client_id: uuid.UUID | None = None,
    ) -> WebhookEndpoint | None:
        query = select(WebhookEndpoint).where(WebhookEndpoint.id == endpoint_id)
        if client_id is not None:
            query = query.where(WebhookEndpoint.client_id == client_id)
        return await session.scalar(query)

    async def create(
        self,
        session: AsyncSession,
        *,
        client_id: uuid.UUID,
        name: str,
        url: str,
        events: list[str],
        enabled: bool,
        is_default: bool,
    ) -> CreatedWebhookEndpoint:
        self._validate_events(events)
        await validate_webhook_destination(
            url,
            allow_private=self.settings.webhook_allow_private_addresses,
        )
        signing_secret = generate_webhook_signing_secret()
        endpoint = WebhookEndpoint(
            client_id=client_id,
            name=name,
            url=url,
            encrypted_signing_secret=self.cipher.encrypt(signing_secret),
            signing_secret_hint=credential_hint(signing_secret),
            events=events,
            enabled=enabled,
            is_default=is_default and enabled,
        )
        if endpoint.is_default:
            await self._clear_default(session, client_id)
        session.add(endpoint)
        try:
            await session.commit()
        except IntegrityError as exc:
            await session.rollback()
            raise WebhookConflict("A webhook endpoint with that name already exists for this client") from exc
        await session.refresh(endpoint)
        return CreatedWebhookEndpoint(endpoint=endpoint, signing_secret=signing_secret)

    async def update(
        self,
        session: AsyncSession,
        endpoint: WebhookEndpoint,
        *,
        name: str | None,
        url: str | None,
        events: list[str] | None,
        enabled: bool | None,
        is_default: bool | None,
    ) -> WebhookEndpoint:
        if url is not None:
            await validate_webhook_destination(
                url,
                allow_private=self.settings.webhook_allow_private_addresses,
            )
            endpoint.url = url
        if events is not None:
            self._validate_events(events)
            endpoint.events = events
        if name is not None:
            endpoint.name = name
        if enabled is not None:
            endpoint.enabled = enabled
            if not enabled:
                endpoint.is_default = False
        if is_default is not None:
            if is_default and not endpoint.enabled:
                raise WebhookConflict("A disabled webhook endpoint cannot be the client default")
            if is_default:
                await self._clear_default(session, endpoint.client_id, except_id=endpoint.id)
            endpoint.is_default = is_default
        try:
            await session.commit()
        except IntegrityError as exc:
            await session.rollback()
            raise WebhookConflict("A webhook endpoint with that name already exists for this client") from exc
        await session.refresh(endpoint)
        return endpoint

    async def disable(self, session: AsyncSession, endpoint: WebhookEndpoint) -> WebhookEndpoint:
        endpoint.enabled = False
        endpoint.is_default = False
        await session.commit()
        await session.refresh(endpoint)
        return endpoint

    async def rotate_secret(
        self,
        session: AsyncSession,
        endpoint: WebhookEndpoint,
    ) -> CreatedWebhookEndpoint:
        signing_secret = generate_webhook_signing_secret()
        endpoint.encrypted_signing_secret = self.cipher.encrypt(signing_secret)
        endpoint.signing_secret_hint = credential_hint(signing_secret)
        await session.commit()
        await session.refresh(endpoint)
        return CreatedWebhookEndpoint(endpoint=endpoint, signing_secret=signing_secret)

    async def resolve_for_job(
        self,
        session: AsyncSession,
        *,
        client_id: uuid.UUID,
        selection: uuid.UUID | bool | None,
    ) -> uuid.UUID | None:
        if selection is False:
            return None
        if isinstance(selection, uuid.UUID):
            endpoint = await session.scalar(
                select(WebhookEndpoint).where(
                    WebhookEndpoint.id == selection,
                    WebhookEndpoint.client_id == client_id,
                    WebhookEndpoint.enabled.is_(True),
                )
            )
            if endpoint is None:
                raise WebhookEndpointNotFound("Enabled webhook endpoint not found for this client")
            return endpoint.id
        endpoint = await session.scalar(
            select(WebhookEndpoint).where(
                WebhookEndpoint.client_id == client_id,
                WebhookEndpoint.enabled.is_(True),
                WebhookEndpoint.is_default.is_(True),
            )
        )
        return endpoint.id if endpoint is not None else None

    async def create_test_event(
        self,
        session: AsyncSession,
        endpoint: WebhookEndpoint,
    ) -> WebhookEvent:
        if not endpoint.enabled:
            raise WebhookConflict("Enable the webhook endpoint before sending a test event")
        now = datetime.now(UTC)
        event_id = uuid.uuid4()
        event = WebhookEvent(
            id=event_id,
            client_id=endpoint.client_id,
            endpoint_id=endpoint.id,
            job_request_id=None,
            event_type="webhook.test",
            payload={
                "api_version": "2",
                "event_id": str(event_id),
                "event_type": "webhook.test",
                "created_at": now.isoformat(),
                "data": {
                    "client_id": str(endpoint.client_id),
                    "endpoint_id": str(endpoint.id),
                },
            },
            status="pending",
            attempt_count=0,
            next_attempt_at=now,
        )
        session.add(event)
        await session.commit()
        await session.refresh(event)
        return event

    @staticmethod
    async def _clear_default(
        session: AsyncSession,
        client_id: uuid.UUID,
        *,
        except_id: uuid.UUID | None = None,
    ) -> None:
        statement = update(WebhookEndpoint).where(
            WebhookEndpoint.client_id == client_id,
            WebhookEndpoint.is_default.is_(True),
        )
        if except_id is not None:
            statement = statement.where(WebhookEndpoint.id != except_id)
        await session.execute(statement.values(is_default=False, updated_at=datetime.now(UTC)))

    @staticmethod
    def _validate_events(events: list[str]) -> None:
        if not events:
            raise WebhookConflict("At least one webhook event type is required")
        unsupported = sorted(set(events) - set(JOB_WEBHOOK_EVENT_TYPES))
        if unsupported:
            raise WebhookConflict(f"Unsupported webhook event types: {', '.join(unsupported)}")
        if len(events) != len(set(events)):
            raise WebhookConflict("Webhook event types must not contain duplicates")


async def enqueue_job_webhook_event(
    session: AsyncSession,
    *,
    job: JobRequest,
    run: PipelineRun,
) -> uuid.UUID | None:
    """Add a terminal event to the current database transaction, without network I/O."""

    if job.webhook_endpoint_id is None or job.status not in {"completed", "failed"}:
        return None
    event_type = f"job.{job.status}"
    endpoint = await session.scalar(
        select(WebhookEndpoint).where(
            WebhookEndpoint.id == job.webhook_endpoint_id,
            WebhookEndpoint.enabled.is_(True),
        )
    )
    if endpoint is None or event_type not in endpoint.events:
        return None

    now = datetime.now(UTC)
    event_id = uuid.uuid4()
    payload = {
        "api_version": "2",
        "event_id": str(event_id),
        "event_type": event_type,
        "created_at": now.isoformat(),
        "data": {
            "job_id": str(job.id),
            "client_job_id": job.client_job_id,
            "asset_id": str(job.asset_id),
            "pipeline_run_id": str(job.pipeline_run_id),
            "pipeline": run.pipeline_name,
            "pipeline_version": run.pipeline_version,
            "status": job.status,
            "cache_hit": job.cache_hit,
            "run_reused": job.run_reused,
            "manifest_path": f"/v2/jobs/{job.id}/manifest",
        },
    }
    statement = (
        insert(WebhookEvent)
        .values(
            id=event_id,
            client_id=job.client_id,
            endpoint_id=endpoint.id,
            job_request_id=job.id,
            event_type=event_type,
            payload=payload,
            status="pending",
            attempt_count=0,
            next_attempt_at=now,
        )
        .on_conflict_do_nothing(constraint="uq_webhook_event_job_type")
        .returning(WebhookEvent.id)
    )
    return (await session.execute(statement)).scalar_one_or_none()


class WebhookDispatcher:
    def __init__(
        self,
        settings: Settings,
        cipher: CredentialCipher,
        session_factory: async_sessionmaker[AsyncSession],
        client: httpx.AsyncClient,
    ) -> None:
        self.settings = settings
        self.cipher = cipher
        self.session_factory = session_factory
        self.client = client

    async def validate_credentials(self) -> None:
        async with self.session_factory() as session:
            values = await session.scalars(select(WebhookEndpoint.encrypted_signing_secret))
            for value in values:
                self.cipher.decrypt(value)

    async def dispatch_once(self) -> bool:
        claimed = await self._claim()
        if claimed is None:
            return False
        try:
            await validate_webhook_destination(
                claimed.url,
                allow_private=self.settings.webhook_allow_private_addresses,
            )
        except WebhookDestinationRejected as exc:
            now = datetime.now(UTC)
            result = DeliveryResult(
                outcome="rejected",
                retryable=False,
                started_at=now,
                completed_at=now,
                duration_ms=0,
                error_message=str(exc),
            )
        else:
            result = await self._deliver(claimed)
        await self._finalize(claimed, result)
        return True

    async def _claim(self) -> ClaimedWebhook | None:
        now = datetime.now(UTC)
        async with self.session_factory() as session:
            event = await session.scalar(
                select(WebhookEvent)
                .where(
                    or_(
                        (
                            WebhookEvent.status.in_({"pending", "retrying"})
                            & (WebhookEvent.next_attempt_at <= now)
                        ),
                        (
                            (WebhookEvent.status == "processing")
                            & (WebhookEvent.lease_expires_at.is_not(None))
                            & (WebhookEvent.lease_expires_at <= now)
                        ),
                    )
                )
                .order_by(WebhookEvent.next_attempt_at, WebhookEvent.created_at)
                .limit(1)
                .with_for_update(skip_locked=True)
            )
            if event is None:
                return None
            endpoint = await session.get(WebhookEndpoint, event.endpoint_id)
            if endpoint is None or not endpoint.enabled:
                event.status = "abandoned"
                event.abandoned_at = now
                event.last_error = "Webhook endpoint is disabled"
                event.lease_token = None
                event.lease_expires_at = None
                await session.commit()
                return None

            lease_token = uuid.uuid4()
            event.status = "processing"
            event.lease_token = lease_token
            lease_seconds = max(
                self.settings.webhook_lease_seconds,
                math.ceil(self.settings.webhook_timeout_seconds) + 5,
            )
            event.lease_expires_at = now + timedelta(seconds=lease_seconds)
            event.updated_at = now
            signing_secret = self.cipher.decrypt(endpoint.encrypted_signing_secret)
            claimed = ClaimedWebhook(
                event_id=event.id,
                endpoint_id=event.endpoint_id,
                attempt_number=event.attempt_count + 1,
                lease_token=lease_token,
                url=endpoint.url,
                signing_secret=signing_secret,
                payload=dict(event.payload),
            )
            await session.commit()
            return claimed

    async def _deliver(self, claimed: ClaimedWebhook) -> DeliveryResult:
        body = serialize_webhook_payload(claimed.payload)
        timestamp = int(time.time())
        headers = {
            "Content-Type": "application/json",
            "User-Agent": "media-engine-webhooks/2",
            "X-Media-Engine-Event-Id": str(claimed.event_id),
            "X-Media-Engine-Timestamp": str(timestamp),
            "X-Media-Engine-Signature": webhook_signature(
                claimed.signing_secret,
                timestamp,
                body,
            ),
        }
        started_at = datetime.now(UTC)
        started = time.monotonic()
        try:
            response = await self.client.post(claimed.url, content=body, headers=headers)
        except httpx.HTTPError as exc:
            completed_at = datetime.now(UTC)
            return DeliveryResult(
                outcome="network_error",
                retryable=True,
                started_at=started_at,
                completed_at=completed_at,
                duration_ms=max(0, round((time.monotonic() - started) * 1000)),
                error_message=f"{type(exc).__name__}: {exc}",
            )

        completed_at = datetime.now(UTC)
        status_code = response.status_code
        success = 200 <= status_code < 300
        retryable = status_code in {408, 429} or status_code >= 500
        retry_after = self._retry_after(response) if retryable else None
        preview = response.content[: self.settings.webhook_response_preview_bytes].decode(
            "utf-8", errors="replace"
        )
        return DeliveryResult(
            outcome="delivered" if success else ("retryable_http" if retryable else "permanent_http"),
            retryable=retryable,
            started_at=started_at,
            completed_at=completed_at,
            duration_ms=max(0, round((time.monotonic() - started) * 1000)),
            response_status=status_code,
            response_body_preview=preview or None,
            error_message=None if success else f"Webhook endpoint returned HTTP {status_code}",
            retry_after_seconds=retry_after,
        )

    async def _finalize(self, claimed: ClaimedWebhook, result: DeliveryResult) -> None:
        async with self.session_factory() as session:
            event = await session.scalar(
                select(WebhookEvent)
                .where(
                    WebhookEvent.id == claimed.event_id,
                    WebhookEvent.status == "processing",
                    WebhookEvent.lease_token == claimed.lease_token,
                )
                .with_for_update()
            )
            if event is None:
                return
            event.attempt_count = claimed.attempt_number
            event.last_error = result.error_message
            event.lease_token = None
            event.lease_expires_at = None
            event.updated_at = result.completed_at
            session.add(
                WebhookDeliveryAttempt(
                    webhook_event_id=event.id,
                    attempt_number=claimed.attempt_number,
                    started_at=result.started_at,
                    completed_at=result.completed_at,
                    duration_ms=result.duration_ms,
                    response_status=result.response_status,
                    response_body_preview=result.response_body_preview,
                    error_message=result.error_message,
                    outcome=result.outcome,
                )
            )
            if result.outcome == "delivered":
                event.status = "delivered"
                event.delivered_at = result.completed_at
            elif result.retryable and claimed.attempt_number < self.settings.webhook_max_attempts:
                event.status = "retrying"
                delay = min(
                    self.settings.webhook_initial_backoff_seconds * (2 ** (claimed.attempt_number - 1)),
                    self.settings.webhook_max_backoff_seconds,
                )
                if result.retry_after_seconds is not None:
                    delay = min(
                        max(delay, result.retry_after_seconds),
                        self.settings.webhook_max_backoff_seconds,
                    )
                event.next_attempt_at = result.completed_at + timedelta(seconds=delay)
            else:
                event.status = "abandoned"
                event.abandoned_at = result.completed_at
            await session.commit()

    @staticmethod
    def _retry_after(response: httpx.Response) -> int | None:
        value = response.headers.get("Retry-After")
        if value is None:
            return None
        try:
            return max(0, int(value))
        except ValueError:
            return None
