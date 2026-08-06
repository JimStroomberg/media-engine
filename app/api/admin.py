from __future__ import annotations

import uuid
from datetime import datetime
from decimal import Decimal
from typing import Annotated, Any, Literal

from fastapi import APIRouter, Depends, HTTPException, Query, Response, status
from pydantic import AnyHttpUrl, BaseModel, ConfigDict, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from .. import __version__
from ..config import Settings, get_settings
from ..persistence.models import (
    AIUsageEvent,
    ApiKey,
    ClientProject,
    ProviderConfig,
    WebhookDeliveryAttempt,
    WebhookEndpoint,
    WebhookEvent,
    Worker,
)
from ..security import CredentialCipher
from ..services.access import ALL_CLIENT_SCOPES, AccessService, ApiKeyConflict, ClientConflict
from ..services.providers import (
    PROVIDER_MODELS,
    ProviderConfigurationService,
    ProviderConflict,
    ProviderVerificationFailed,
)
from ..services.webhooks import (
    JOB_WEBHOOK_EVENT_TYPES,
    WebhookConflict,
    WebhookDestinationRejected,
    WebhookEndpointService,
)
from ..services.worker_access import (
    WorkerAccessService,
    WorkerRemovalConflict,
    WorkerStateConflict,
)
from .auth import get_credential_cipher, require_admin
from .v2 import database_session

router = APIRouter(
    prefix="/v2/admin",
    tags=["management"],
    dependencies=[Depends(require_admin)],
)


class ProviderModels(BaseModel):
    model_config = ConfigDict(extra="forbid")

    transcription: str = Field(min_length=1, max_length=128)
    planning: str = Field(min_length=1, max_length=128)
    vision: str = Field(min_length=1, max_length=128)
    summary: str = Field(min_length=1, max_length=128)


class ProviderCreateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=1, max_length=255)
    provider: Literal["openai", "xai"]
    api_key: str = Field(min_length=8, max_length=4096)
    base_url: AnyHttpUrl | None = None
    models: ProviderModels | None = None
    timeout_seconds: float = Field(600, ge=10, le=3600)
    max_retries: int = Field(2, ge=0, le=10)
    enabled: bool = True
    is_default: bool = False


class ProviderUpdateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str | None = Field(default=None, min_length=1, max_length=255)
    api_key: str | None = Field(default=None, min_length=8, max_length=4096)
    base_url: AnyHttpUrl | None = None
    models: ProviderModels | None = None
    timeout_seconds: float | None = Field(default=None, ge=10, le=3600)
    max_retries: int | None = Field(default=None, ge=0, le=10)
    enabled: bool | None = None
    is_default: bool | None = None


class ProviderResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    provider_config_id: uuid.UUID
    name: str
    provider: str
    credential_hint: str
    has_credentials: bool
    base_url: str
    models: dict[str, str]
    timeout_seconds: float
    max_retries: int
    enabled: bool
    is_default: bool
    last_verified_at: datetime | None
    last_error: str | None
    created_at: datetime
    updated_at: datetime


class ClientCreateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=1, max_length=255)
    enabled: bool = True


class ClientResponse(BaseModel):
    client_id: uuid.UUID
    name: str
    enabled: bool
    created_at: datetime
    updated_at: datetime


class ApiKeyCreateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=1, max_length=255)
    scopes: list[Literal["assets:read", "assets:write", "jobs:read", "jobs:write"]] = Field(
        default_factory=lambda: sorted(ALL_CLIENT_SCOPES),
        min_length=1,
    )
    expires_at: datetime | None = None


class ApiKeyResponse(BaseModel):
    api_key_id: uuid.UUID
    client_id: uuid.UUID
    name: str
    key_prefix: str
    scopes: list[str]
    created_at: datetime
    expires_at: datetime | None
    last_used_at: datetime | None
    revoked_at: datetime | None


class ApiKeyCreatedResponse(ApiKeyResponse):
    api_key: str


class WorkerCreateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    display_name: str = Field(min_length=1, max_length=255)
    profile: str = Field(default="cpu", pattern=r"^[a-z0-9][a-z0-9._-]{0,63}$")
    expires_at: datetime | None = None


class WorkerUpdateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    display_name: str | None = Field(default=None, min_length=1, max_length=255)
    profile: str | None = Field(default=None, pattern=r"^[a-z0-9][a-z0-9._-]{0,63}$")


class WorkerTokenRotateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    expires_at: datetime | None = None


class ManagedWorkerResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    worker_id: uuid.UUID
    worker_key: str
    display_name: str
    profile: str
    status: str
    desired_state: str
    credential_prefix: str | None
    credential_created_at: datetime | None
    credential_expires_at: datetime | None
    credential_last_used_at: datetime | None
    credential_revoked_at: datetime | None
    created_at: datetime


class ManagedWorkerCreatedResponse(ManagedWorkerResponse):
    worker_token: str
    worker_api_url: str | None
    release_version: str


class UsageEventResponse(BaseModel):
    event_id: uuid.UUID
    stage_run_id: uuid.UUID
    stage_attempt: int
    provider: str
    model: str
    operation: str
    outcome: str
    usage: dict[str, Any]
    cost_usd_ticks: int | None
    estimated_cost_usd: Decimal | None
    latency_ms: int
    started_at: datetime
    completed_at: datetime


class WebhookEndpointCreateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=1, max_length=255)
    url: AnyHttpUrl
    events: list[Literal["job.completed", "job.failed"]] = Field(
        default_factory=lambda: list(JOB_WEBHOOK_EVENT_TYPES),
        min_length=1,
    )
    enabled: bool = True
    is_default: bool = False


class WebhookEndpointUpdateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str | None = Field(default=None, min_length=1, max_length=255)
    url: AnyHttpUrl | None = None
    events: list[Literal["job.completed", "job.failed"]] | None = Field(default=None, min_length=1)
    enabled: bool | None = None
    is_default: bool | None = None


class WebhookEndpointResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    webhook_endpoint_id: uuid.UUID
    client_id: uuid.UUID
    name: str
    url: str
    events: list[str]
    signing_secret_hint: str
    enabled: bool
    is_default: bool
    created_at: datetime
    updated_at: datetime


class WebhookEndpointCreatedResponse(WebhookEndpointResponse):
    signing_secret: str


class WebhookDeliveryAttemptResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    attempt_id: uuid.UUID
    attempt_number: int
    started_at: datetime
    completed_at: datetime
    duration_ms: int
    response_status: int | None
    response_body_preview: str | None
    error_message: str | None
    outcome: str


class WebhookEventResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    event_id: uuid.UUID
    client_id: uuid.UUID
    webhook_endpoint_id: uuid.UUID
    job_id: uuid.UUID | None
    event_type: str
    payload: dict[str, Any]
    status: str
    attempt_count: int
    next_attempt_at: datetime
    delivered_at: datetime | None
    abandoned_at: datetime | None
    last_error: str | None
    created_at: datetime
    updated_at: datetime
    attempts: list[WebhookDeliveryAttemptResponse]


def provider_response(config: ProviderConfig) -> ProviderResponse:
    runtime = config.runtime_options
    return ProviderResponse(
        provider_config_id=config.id,
        name=config.name,
        provider=config.provider,
        credential_hint=config.credential_hint,
        has_credentials=True,
        base_url=config.base_url,
        models=dict(config.models),
        timeout_seconds=float(runtime.get("timeout_seconds", 600)),
        max_retries=int(runtime.get("max_retries", 2)),
        enabled=config.enabled,
        is_default=config.is_default,
        last_verified_at=config.last_verified_at,
        last_error=config.last_error,
        created_at=config.created_at,
        updated_at=config.updated_at,
    )


def client_response(client: ClientProject) -> ClientResponse:
    return ClientResponse(
        client_id=client.id,
        name=client.name,
        enabled=client.enabled,
        created_at=client.created_at,
        updated_at=client.updated_at,
    )


def api_key_response(api_key: ApiKey) -> ApiKeyResponse:
    return ApiKeyResponse(
        api_key_id=api_key.id,
        client_id=api_key.client_id,
        name=api_key.name,
        key_prefix=api_key.key_prefix,
        scopes=list(api_key.scopes),
        created_at=api_key.created_at,
        expires_at=api_key.expires_at,
        last_used_at=api_key.last_used_at,
        revoked_at=api_key.revoked_at,
    )


def managed_worker_response(worker: Worker) -> ManagedWorkerResponse:
    return ManagedWorkerResponse(
        worker_id=worker.id,
        worker_key=worker.worker_key,
        display_name=worker.display_name,
        profile=worker.profile,
        status=worker.status,
        desired_state=worker.desired_state,
        credential_prefix=worker.credential_prefix,
        credential_created_at=worker.credential_created_at,
        credential_expires_at=worker.credential_expires_at,
        credential_last_used_at=worker.credential_last_used_at,
        credential_revoked_at=worker.credential_revoked_at,
        created_at=worker.created_at,
    )


async def managed_worker_or_404(session: AsyncSession, worker_id: uuid.UUID) -> Worker:
    worker = await session.scalar(
        select(Worker).where(Worker.id == worker_id, Worker.removed_at.is_(None))
    )
    if worker is None:
        raise HTTPException(status_code=404, detail="Worker not found")
    return worker


def webhook_endpoint_response(endpoint: WebhookEndpoint) -> WebhookEndpointResponse:
    return WebhookEndpointResponse(
        webhook_endpoint_id=endpoint.id,
        client_id=endpoint.client_id,
        name=endpoint.name,
        url=endpoint.url,
        events=list(endpoint.events),
        signing_secret_hint=endpoint.signing_secret_hint,
        enabled=endpoint.enabled,
        is_default=endpoint.is_default,
        created_at=endpoint.created_at,
        updated_at=endpoint.updated_at,
    )


def webhook_attempt_response(attempt: WebhookDeliveryAttempt) -> WebhookDeliveryAttemptResponse:
    return WebhookDeliveryAttemptResponse(
        attempt_id=attempt.id,
        attempt_number=attempt.attempt_number,
        started_at=attempt.started_at,
        completed_at=attempt.completed_at,
        duration_ms=attempt.duration_ms,
        response_status=attempt.response_status,
        response_body_preview=attempt.response_body_preview,
        error_message=attempt.error_message,
        outcome=attempt.outcome,
    )


def webhook_event_response(event: WebhookEvent) -> WebhookEventResponse:
    attempts = event.__dict__.get("attempts", [])
    return WebhookEventResponse(
        event_id=event.id,
        client_id=event.client_id,
        webhook_endpoint_id=event.endpoint_id,
        job_id=event.job_request_id,
        event_type=event.event_type,
        payload=dict(event.payload),
        status=event.status,
        attempt_count=event.attempt_count,
        next_attempt_at=event.next_attempt_at,
        delivered_at=event.delivered_at,
        abandoned_at=event.abandoned_at,
        last_error=event.last_error,
        created_at=event.created_at,
        updated_at=event.updated_at,
        attempts=[
            webhook_attempt_response(attempt)
            for attempt in sorted(attempts, key=lambda value: value.attempt_number)
        ],
    )


@router.post("/workers", response_model=ManagedWorkerCreatedResponse, status_code=status.HTTP_201_CREATED)
async def create_worker(
    payload: WorkerCreateRequest,
    settings: Settings = Depends(get_settings),
    session: AsyncSession = Depends(database_session),
) -> ManagedWorkerCreatedResponse:
    worker, generated = await WorkerAccessService().create(
        session,
        display_name=payload.display_name,
        profile=payload.profile,
        expires_at=payload.expires_at,
    )
    response = managed_worker_response(worker)
    return ManagedWorkerCreatedResponse(
        **response.model_dump(),
        worker_token=generated.token,
        worker_api_url=settings.worker_advertised_api_url,
        release_version=__version__,
    )


@router.patch("/workers/{worker_id}", response_model=ManagedWorkerResponse)
async def update_worker(
    worker_id: uuid.UUID,
    payload: WorkerUpdateRequest,
    session: AsyncSession = Depends(database_session),
) -> ManagedWorkerResponse:
    worker = await managed_worker_or_404(session, worker_id)
    worker = await WorkerAccessService().update(
        session,
        worker,
        display_name=payload.display_name,
        profile=payload.profile,
    )
    return managed_worker_response(worker)


@router.post("/workers/{worker_id}/rotate-token", response_model=ManagedWorkerCreatedResponse)
async def rotate_worker_token(
    worker_id: uuid.UUID,
    payload: WorkerTokenRotateRequest,
    settings: Settings = Depends(get_settings),
    session: AsyncSession = Depends(database_session),
) -> ManagedWorkerCreatedResponse:
    worker = await managed_worker_or_404(session, worker_id)
    generated = await WorkerAccessService().rotate(session, worker, expires_at=payload.expires_at)
    response = managed_worker_response(worker)
    return ManagedWorkerCreatedResponse(
        **response.model_dump(),
        worker_token=generated.token,
        worker_api_url=settings.worker_advertised_api_url,
        release_version=__version__,
    )


@router.post("/workers/{worker_id}/drain", response_model=ManagedWorkerResponse)
async def drain_worker(
    worker_id: uuid.UUID,
    session: AsyncSession = Depends(database_session),
) -> ManagedWorkerResponse:
    worker = await managed_worker_or_404(session, worker_id)
    try:
        worker = await WorkerAccessService().set_desired_state(session, worker, "draining")
    except WorkerStateConflict as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return managed_worker_response(worker)


@router.post("/workers/{worker_id}/activate", response_model=ManagedWorkerResponse)
async def activate_worker(
    worker_id: uuid.UUID,
    session: AsyncSession = Depends(database_session),
) -> ManagedWorkerResponse:
    worker = await managed_worker_or_404(session, worker_id)
    try:
        worker = await WorkerAccessService().set_desired_state(session, worker, "active")
    except WorkerStateConflict as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return managed_worker_response(worker)


@router.post("/workers/{worker_id}/revoke", response_model=ManagedWorkerResponse)
async def revoke_worker(
    worker_id: uuid.UUID,
    session: AsyncSession = Depends(database_session),
) -> ManagedWorkerResponse:
    worker = await managed_worker_or_404(session, worker_id)
    worker = await WorkerAccessService().set_desired_state(session, worker, "revoked")
    return managed_worker_response(worker)


@router.delete("/workers/{worker_id}", status_code=status.HTTP_204_NO_CONTENT)
async def remove_worker(
    worker_id: uuid.UUID,
    settings: Settings = Depends(get_settings),
    session: AsyncSession = Depends(database_session),
) -> Response:
    worker = await managed_worker_or_404(session, worker_id)
    if settings.initial_worker_token and worker.worker_key == settings.initial_worker_key:
        raise HTTPException(
            status_code=409,
            detail=(
                "Remove the bundled worker from the deployment configuration before removing its managed record"
            ),
        )
    try:
        await WorkerAccessService().remove(session, worker)
    except WorkerRemovalConflict as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.get("/providers", response_model=list[ProviderResponse])
async def list_providers(
    settings: Settings = Depends(get_settings),
    cipher: CredentialCipher = Depends(get_credential_cipher),
    session: AsyncSession = Depends(database_session),
) -> list[ProviderResponse]:
    configs = await ProviderConfigurationService(settings, cipher).list(session)
    return [provider_response(config) for config in configs]


@router.post("/providers", response_model=ProviderResponse, status_code=status.HTTP_201_CREATED)
async def create_provider(
    payload: ProviderCreateRequest,
    settings: Settings = Depends(get_settings),
    cipher: CredentialCipher = Depends(get_credential_cipher),
    session: AsyncSession = Depends(database_session),
) -> ProviderResponse:
    service = ProviderConfigurationService(settings, cipher)
    try:
        config = await service.create(
            session,
            name=payload.name,
            provider=payload.provider,
            api_key=payload.api_key,
            base_url=str(payload.base_url) if payload.base_url else None,
            models=payload.models.model_dump() if payload.models else PROVIDER_MODELS[payload.provider],
            timeout_seconds=payload.timeout_seconds,
            max_retries=payload.max_retries,
            enabled=payload.enabled,
            is_default=payload.is_default,
        )
    except ProviderConflict as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except ProviderVerificationFailed as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return provider_response(config)


@router.patch("/providers/{provider_config_id}", response_model=ProviderResponse)
async def update_provider(
    provider_config_id: uuid.UUID,
    payload: ProviderUpdateRequest,
    settings: Settings = Depends(get_settings),
    cipher: CredentialCipher = Depends(get_credential_cipher),
    session: AsyncSession = Depends(database_session),
) -> ProviderResponse:
    service = ProviderConfigurationService(settings, cipher)
    config = await service.get(session, provider_config_id)
    if config is None:
        raise HTTPException(status_code=404, detail="Provider connection not found")
    try:
        config = await service.update(
            session,
            config,
            name=payload.name,
            api_key=payload.api_key,
            base_url=str(payload.base_url) if payload.base_url else None,
            models=payload.models.model_dump(exclude_unset=True) if payload.models else None,
            timeout_seconds=payload.timeout_seconds,
            max_retries=payload.max_retries,
            enabled=payload.enabled,
            is_default=payload.is_default,
        )
    except ProviderConflict as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except ProviderVerificationFailed as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return provider_response(config)


@router.post("/providers/{provider_config_id}/verify", response_model=ProviderResponse)
async def verify_provider(
    provider_config_id: uuid.UUID,
    settings: Settings = Depends(get_settings),
    cipher: CredentialCipher = Depends(get_credential_cipher),
    session: AsyncSession = Depends(database_session),
) -> ProviderResponse:
    service = ProviderConfigurationService(settings, cipher)
    config = await service.get(session, provider_config_id)
    if config is None:
        raise HTTPException(status_code=404, detail="Provider connection not found")
    try:
        config = await service.verify(session, config)
    except ProviderVerificationFailed as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return provider_response(config)


@router.delete("/providers/{provider_config_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_provider(
    provider_config_id: uuid.UUID,
    settings: Settings = Depends(get_settings),
    cipher: CredentialCipher = Depends(get_credential_cipher),
    session: AsyncSession = Depends(database_session),
) -> None:
    service = ProviderConfigurationService(settings, cipher)
    config = await service.get(session, provider_config_id)
    if config is None:
        raise HTTPException(status_code=404, detail="Provider connection not found")
    await service.delete(session, config)


@router.get("/clients", response_model=list[ClientResponse])
async def list_clients(session: AsyncSession = Depends(database_session)) -> list[ClientResponse]:
    clients = await AccessService().list_clients(session)
    return [client_response(client) for client in clients]


@router.post("/clients", response_model=ClientResponse, status_code=status.HTTP_201_CREATED)
async def create_client(
    payload: ClientCreateRequest,
    session: AsyncSession = Depends(database_session),
) -> ClientResponse:
    try:
        client = await AccessService().create_client(session, name=payload.name, enabled=payload.enabled)
    except ClientConflict as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return client_response(client)


@router.get("/clients/{client_id}/api-keys", response_model=list[ApiKeyResponse])
async def list_api_keys(
    client_id: uuid.UUID,
    session: AsyncSession = Depends(database_session),
) -> list[ApiKeyResponse]:
    if await session.get(ClientProject, client_id) is None:
        raise HTTPException(status_code=404, detail="Client not found")
    keys = await AccessService().list_api_keys(session, client_id)
    return [api_key_response(api_key) for api_key in keys]


@router.post(
    "/clients/{client_id}/api-keys",
    response_model=ApiKeyCreatedResponse,
    status_code=status.HTTP_201_CREATED,
)
async def create_api_key(
    client_id: uuid.UUID,
    payload: ApiKeyCreateRequest,
    session: AsyncSession = Depends(database_session),
) -> ApiKeyCreatedResponse:
    client = await session.get(ClientProject, client_id)
    if client is None:
        raise HTTPException(status_code=404, detail="Client not found")
    try:
        api_key, generated = await AccessService().create_api_key(
            session,
            client=client,
            name=payload.name,
            scopes=list(payload.scopes),
            expires_at=payload.expires_at,
        )
    except ApiKeyConflict as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    response = api_key_response(api_key)
    return ApiKeyCreatedResponse(**response.model_dump(), api_key=generated.token)


@router.delete("/api-keys/{api_key_id}", response_model=ApiKeyResponse)
async def revoke_api_key(
    api_key_id: uuid.UUID,
    session: AsyncSession = Depends(database_session),
) -> ApiKeyResponse:
    api_key = await session.get(ApiKey, api_key_id)
    if api_key is None:
        raise HTTPException(status_code=404, detail="API key not found")
    return api_key_response(await AccessService().revoke_api_key(session, api_key))


@router.get(
    "/clients/{client_id}/webhook-endpoints",
    response_model=list[WebhookEndpointResponse],
)
async def list_webhook_endpoints(
    client_id: uuid.UUID,
    settings: Settings = Depends(get_settings),
    cipher: CredentialCipher = Depends(get_credential_cipher),
    session: AsyncSession = Depends(database_session),
) -> list[WebhookEndpointResponse]:
    if await session.get(ClientProject, client_id) is None:
        raise HTTPException(status_code=404, detail="Client not found")
    endpoints = await WebhookEndpointService(settings, cipher).list(session, client_id)
    return [webhook_endpoint_response(endpoint) for endpoint in endpoints]


@router.post(
    "/clients/{client_id}/webhook-endpoints",
    response_model=WebhookEndpointCreatedResponse,
    status_code=status.HTTP_201_CREATED,
)
async def create_webhook_endpoint(
    client_id: uuid.UUID,
    payload: WebhookEndpointCreateRequest,
    settings: Settings = Depends(get_settings),
    cipher: CredentialCipher = Depends(get_credential_cipher),
    session: AsyncSession = Depends(database_session),
) -> WebhookEndpointCreatedResponse:
    if await session.get(ClientProject, client_id) is None:
        raise HTTPException(status_code=404, detail="Client not found")
    try:
        created = await WebhookEndpointService(settings, cipher).create(
            session,
            client_id=client_id,
            name=payload.name,
            url=str(payload.url),
            events=list(payload.events),
            enabled=payload.enabled,
            is_default=payload.is_default,
        )
    except WebhookConflict as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except WebhookDestinationRejected as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    base = webhook_endpoint_response(created.endpoint)
    return WebhookEndpointCreatedResponse(
        **base.model_dump(),
        signing_secret=created.signing_secret,
    )


@router.patch("/webhook-endpoints/{endpoint_id}", response_model=WebhookEndpointResponse)
async def update_webhook_endpoint(
    endpoint_id: uuid.UUID,
    payload: WebhookEndpointUpdateRequest,
    settings: Settings = Depends(get_settings),
    cipher: CredentialCipher = Depends(get_credential_cipher),
    session: AsyncSession = Depends(database_session),
) -> WebhookEndpointResponse:
    service = WebhookEndpointService(settings, cipher)
    endpoint = await service.get(session, endpoint_id)
    if endpoint is None:
        raise HTTPException(status_code=404, detail="Webhook endpoint not found")
    try:
        endpoint = await service.update(
            session,
            endpoint,
            name=payload.name,
            url=str(payload.url) if payload.url is not None else None,
            events=list(payload.events) if payload.events is not None else None,
            enabled=payload.enabled,
            is_default=payload.is_default,
        )
    except WebhookConflict as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except WebhookDestinationRejected as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return webhook_endpoint_response(endpoint)


@router.delete("/webhook-endpoints/{endpoint_id}", response_model=WebhookEndpointResponse)
async def disable_webhook_endpoint(
    endpoint_id: uuid.UUID,
    settings: Settings = Depends(get_settings),
    cipher: CredentialCipher = Depends(get_credential_cipher),
    session: AsyncSession = Depends(database_session),
) -> WebhookEndpointResponse:
    service = WebhookEndpointService(settings, cipher)
    endpoint = await service.get(session, endpoint_id)
    if endpoint is None:
        raise HTTPException(status_code=404, detail="Webhook endpoint not found")
    return webhook_endpoint_response(await service.disable(session, endpoint))


@router.post(
    "/webhook-endpoints/{endpoint_id}/rotate-secret",
    response_model=WebhookEndpointCreatedResponse,
)
async def rotate_webhook_endpoint_secret(
    endpoint_id: uuid.UUID,
    settings: Settings = Depends(get_settings),
    cipher: CredentialCipher = Depends(get_credential_cipher),
    session: AsyncSession = Depends(database_session),
) -> WebhookEndpointCreatedResponse:
    service = WebhookEndpointService(settings, cipher)
    endpoint = await service.get(session, endpoint_id)
    if endpoint is None:
        raise HTTPException(status_code=404, detail="Webhook endpoint not found")
    rotated = await service.rotate_secret(session, endpoint)
    base = webhook_endpoint_response(rotated.endpoint)
    return WebhookEndpointCreatedResponse(
        **base.model_dump(),
        signing_secret=rotated.signing_secret,
    )


@router.post(
    "/webhook-endpoints/{endpoint_id}/test",
    response_model=WebhookEventResponse,
    status_code=status.HTTP_202_ACCEPTED,
)
async def test_webhook_endpoint(
    endpoint_id: uuid.UUID,
    settings: Settings = Depends(get_settings),
    cipher: CredentialCipher = Depends(get_credential_cipher),
    session: AsyncSession = Depends(database_session),
) -> WebhookEventResponse:
    service = WebhookEndpointService(settings, cipher)
    endpoint = await service.get(session, endpoint_id)
    if endpoint is None:
        raise HTTPException(status_code=404, detail="Webhook endpoint not found")
    try:
        event = await service.create_test_event(session, endpoint)
    except WebhookConflict as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return webhook_event_response(event)


@router.get("/webhook-events", response_model=list[WebhookEventResponse])
async def list_webhook_events(
    client_id: uuid.UUID | None = None,
    endpoint_id: uuid.UUID | None = None,
    delivery_status: Annotated[str | None, Query(alias="status", max_length=32)] = None,
    limit: Annotated[int, Query(ge=1, le=500)] = 100,
    session: AsyncSession = Depends(database_session),
) -> list[WebhookEventResponse]:
    query = (
        select(WebhookEvent)
        .options(selectinload(WebhookEvent.attempts))
        .order_by(WebhookEvent.created_at.desc())
        .limit(limit)
    )
    if client_id is not None:
        query = query.where(WebhookEvent.client_id == client_id)
    if endpoint_id is not None:
        query = query.where(WebhookEvent.endpoint_id == endpoint_id)
    if delivery_status is not None:
        query = query.where(WebhookEvent.status == delivery_status)
    events = list(await session.scalars(query))
    return [webhook_event_response(event) for event in events]


@router.get("/webhook-events/{event_id}", response_model=WebhookEventResponse)
async def get_webhook_event(
    event_id: uuid.UUID,
    session: AsyncSession = Depends(database_session),
) -> WebhookEventResponse:
    event = await session.scalar(
        select(WebhookEvent)
        .where(WebhookEvent.id == event_id)
        .options(selectinload(WebhookEvent.attempts))
    )
    if event is None:
        raise HTTPException(status_code=404, detail="Webhook event not found")
    return webhook_event_response(event)


@router.get("/usage", response_model=list[UsageEventResponse])
async def list_usage_events(
    provider: Annotated[str | None, Query(max_length=32)] = None,
    limit: Annotated[int, Query(ge=1, le=500)] = 100,
    session: AsyncSession = Depends(database_session),
) -> list[UsageEventResponse]:
    query = select(AIUsageEvent).order_by(AIUsageEvent.created_at.desc()).limit(limit)
    if provider:
        query = query.where(AIUsageEvent.provider == provider)
    events = list(await session.scalars(query))
    return [
        UsageEventResponse(
            event_id=event.id,
            stage_run_id=event.stage_run_id,
            stage_attempt=event.stage_attempt,
            provider=event.provider,
            model=event.model,
            operation=event.operation,
            outcome=event.outcome,
            usage=event.usage,
            cost_usd_ticks=event.cost_usd_ticks,
            estimated_cost_usd=event.estimated_cost_usd,
            latency_ms=event.latency_ms,
            started_at=event.started_at,
            completed_at=event.completed_at,
        )
        for event in events
    ]
