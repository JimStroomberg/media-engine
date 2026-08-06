from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, Response, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy.ext.asyncio import AsyncSession

from ..config import Settings, get_settings
from ..domain.pipelines import get_pipeline
from ..persistence.models import Artifact, StageRun, Worker
from ..security import CredentialCipher
from ..services.providers import ProviderConfigurationService, RuntimeProviderConnection
from ..services.worker_access import WorkerAccessService, WorkerPrincipal
from ..services.workers import (
    ArtifactCompletion,
    ArtifactRejected,
    LeaseLost,
    UsageCompletion,
    WorkerNotFound,
    WorkerService,
)
from ..storage.s3 import S3Store
from .auth import get_credential_cipher
from .v2 import database_session, get_s3_store

worker_bearer = HTTPBearer(auto_error=False)


async def require_worker_auth(
    credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(worker_bearer)],
    session: AsyncSession = Depends(database_session),
) -> WorkerPrincipal:
    if credentials is None or credentials.scheme.lower() != "bearer":
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="A worker token is required",
            headers={"WWW-Authenticate": "Bearer"},
        )
    principal = await WorkerAccessService().authenticate(session, credentials.credentials)
    if principal is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid, expired, or revoked worker credentials",
            headers={"WWW-Authenticate": "Bearer"},
        )
    return principal


router = APIRouter(prefix="/v2/internal", tags=["internal-workers"])


class WorkerRegisterRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    capabilities: dict[str, list[str]]
    runtime: dict[str, Any] = Field(default_factory=dict)


class WorkerResponse(BaseModel):
    worker_id: uuid.UUID
    worker_key: str
    display_name: str
    profile: str
    capabilities: dict[str, Any]
    runtime: dict[str, Any]
    status: str
    desired_state: str
    created_at: datetime
    registered_at: datetime | None
    last_seen_at: datetime | None


class StageSourceResponse(BaseModel):
    download_url: str
    download_url_expires_at: datetime
    sha256: str
    size_bytes: int
    media_type: str | None


class StageArtifactInputResponse(StageSourceResponse):
    artifact_type: str
    format: str
    schema_version: str
    producer_stage: str


class StageOutputContractResponse(BaseModel):
    artifact_type: str
    required: bool
    schema_version: str


class ProviderConnectionResponse(BaseModel):
    provider_config_id: uuid.UUID
    provider: str
    api_key: str
    base_url: str
    models: dict[str, str]
    timeout_seconds: float
    max_retries: int


class StageClaimResponse(BaseModel):
    stage_id: uuid.UUID
    pipeline_run_id: uuid.UUID
    lease_token: uuid.UUID
    lease_expires_at: datetime
    stage_name: str
    processor: str
    pipeline: str
    pipeline_version: str
    options: dict[str, Any]
    outputs: list[StageOutputContractResponse]
    source: StageSourceResponse
    inputs: list[StageArtifactInputResponse]
    provider_connection: ProviderConnectionResponse | None = None


class ProviderUsageRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    event_id: uuid.UUID
    provider: str = Field(min_length=1, max_length=32)
    model: str = Field(min_length=1, max_length=128)
    operation: str = Field(min_length=1, max_length=64)
    outcome: str = Field(min_length=1, max_length=32)
    usage: dict[str, Any]
    latency_ms: int = Field(ge=0)
    started_at: datetime
    completed_at: datetime


class StageHeartbeatRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    lease_token: uuid.UUID
    progress: float | None = Field(default=None, ge=0.0, le=1.0)


class ArtifactUploadPrepareRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    lease_token: uuid.UUID
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    size_bytes: int = Field(gt=0)
    media_type: str | None = Field(default=None, max_length=255)
    artifact_type: str = Field(min_length=1, max_length=128)
    format: str = Field(min_length=1, max_length=64)
    schema_version: str = Field(default="1", min_length=1, max_length=64)


class ArtifactUploadPrepareResponse(BaseModel):
    upload_required: bool
    upload_url: str | None
    headers: dict[str, str]
    expires_at: datetime


class ArtifactCompletionRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    size_bytes: int = Field(gt=0)
    media_type: str | None = Field(default=None, max_length=255)
    artifact_type: str = Field(min_length=1, max_length=128)
    format: str = Field(min_length=1, max_length=64)
    schema_version: str = Field(default="1", min_length=1, max_length=64)


class StageCompleteRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    lease_token: uuid.UUID
    artifacts: list[ArtifactCompletionRequest] = Field(min_length=1, max_length=32)
    usage_events: list[ProviderUsageRequest] = Field(default_factory=list, max_length=100)


class StageFailRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    lease_token: uuid.UUID
    error_message: str = Field(min_length=1, max_length=4000)
    usage_events: list[ProviderUsageRequest] = Field(default_factory=list, max_length=100)


class StageStatusResponse(BaseModel):
    stage_id: uuid.UUID
    status: str
    attempt: int
    progress: float | None
    lease_expires_at: datetime | None
    error_message: str | None


def worker_response(worker: Worker) -> WorkerResponse:
    return WorkerResponse(
        worker_id=worker.id,
        worker_key=worker.worker_key,
        display_name=worker.display_name,
        profile=worker.profile,
        capabilities=dict(worker.capabilities),
        runtime=dict(worker.runtime),
        status=worker.status,
        desired_state=worker.desired_state,
        created_at=worker.created_at,
        registered_at=worker.registered_at,
        last_seen_at=worker.last_seen_at,
    )


def claim_response(
    stage: StageRun,
    inputs: list[Artifact],
    provider_connection: RuntimeProviderConnection | None,
    *,
    store: S3Store,
    expires_in: int,
) -> StageClaimResponse:
    run = stage.pipeline_run
    source = run.source_blob
    pipeline = get_pipeline(run.pipeline_name, run.pipeline_version)
    stage_definition = pipeline.stage(stage.stage_name)
    if stage.lease_token is None or stage.lease_expires_at is None:
        raise RuntimeError("Claimed stage has no active lease")
    url_expires_at = datetime.now(UTC) + timedelta(seconds=expires_in)
    return StageClaimResponse(
        stage_id=stage.id,
        pipeline_run_id=run.id,
        lease_token=stage.lease_token,
        lease_expires_at=stage.lease_expires_at,
        stage_name=stage.stage_name,
        processor=stage_definition.processor,
        pipeline=run.pipeline_name,
        pipeline_version=run.pipeline_version,
        options=run.options,
        outputs=[
            StageOutputContractResponse(
                artifact_type=output.artifact_type,
                required=output.required,
                schema_version=output.schema_version,
            )
            for output in stage_definition.outputs
        ],
        source=StageSourceResponse(
            download_url=store.create_worker_download_url(source.object_key, expires_in=expires_in),
            download_url_expires_at=url_expires_at,
            sha256=source.sha256,
            size_bytes=source.size_bytes,
            media_type=source.media_type,
        ),
        inputs=[
            StageArtifactInputResponse(
                download_url=store.create_worker_download_url(artifact.blob.object_key, expires_in=expires_in),
                download_url_expires_at=url_expires_at,
                sha256=artifact.blob.sha256,
                size_bytes=artifact.blob.size_bytes,
                media_type=artifact.blob.media_type,
                artifact_type=artifact.artifact_type,
                format=artifact.format,
                schema_version=artifact.schema_version,
                producer_stage=artifact.producer_stage.stage_name,
            )
            for artifact in inputs
        ],
        provider_connection=(
            ProviderConnectionResponse(**provider_connection.__dict__)
            if provider_connection is not None
            else None
        ),
    )


def stage_response(stage: StageRun) -> StageStatusResponse:
    return StageStatusResponse(
        stage_id=stage.id,
        status=stage.status,
        attempt=stage.attempt,
        progress=stage.progress,
        lease_expires_at=stage.lease_expires_at,
        error_message=stage.error_message,
    )


@router.post("/workers/register", response_model=WorkerResponse)
async def register_worker(
    payload: WorkerRegisterRequest,
    principal: Annotated[WorkerPrincipal, Depends(require_worker_auth)],
    settings: Settings = Depends(get_settings),
    session: AsyncSession = Depends(database_session),
    store: S3Store = Depends(get_s3_store),
) -> WorkerResponse:
    try:
        worker = await WorkerService(settings, store).register(
            session,
            worker_id=principal.worker_id,
            capabilities=payload.capabilities,
            runtime=payload.runtime,
        )
    except WorkerNotFound as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    return worker_response(worker)


@router.post("/stages/claim", response_model=StageClaimResponse)
async def claim_stage(
    principal: Annotated[WorkerPrincipal, Depends(require_worker_auth)],
    settings: Settings = Depends(get_settings),
    cipher: CredentialCipher = Depends(get_credential_cipher),
    session: AsyncSession = Depends(database_session),
    store: S3Store = Depends(get_s3_store),
):
    try:
        service = WorkerService(settings, store)
        stage = await service.claim(session, worker_id=principal.worker_id)
    except WorkerNotFound as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    if stage is None:
        return Response(status_code=status.HTTP_204_NO_CONTENT)
    inputs = await service.get_stage_inputs(session, stage.id)
    requested_providers = stage.required_capabilities.get("providers", [])
    provider_connection = None
    if requested_providers:
        provider_connection = await ProviderConfigurationService(settings, cipher).runtime_connection(
            session,
            str(requested_providers[0]),
        )
    return claim_response(
        stage,
        inputs,
        provider_connection,
        store=store,
        expires_in=settings.artifact_url_expiry_seconds,
    )


@router.post("/stages/{stage_id}/heartbeat", response_model=StageStatusResponse)
async def heartbeat_stage(
    stage_id: uuid.UUID,
    payload: StageHeartbeatRequest,
    principal: Annotated[WorkerPrincipal, Depends(require_worker_auth)],
    settings: Settings = Depends(get_settings),
    session: AsyncSession = Depends(database_session),
    store: S3Store = Depends(get_s3_store),
) -> StageStatusResponse:
    try:
        stage = await WorkerService(settings, store).heartbeat(
            session,
            worker_id=principal.worker_id,
            stage_id=stage_id,
            lease_token=payload.lease_token,
            progress=payload.progress,
        )
    except LeaseLost as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return stage_response(stage)


@router.post(
    "/stages/{stage_id}/artifacts/prepare-upload",
    response_model=ArtifactUploadPrepareResponse,
)
async def prepare_artifact_upload(
    stage_id: uuid.UUID,
    payload: ArtifactUploadPrepareRequest,
    principal: Annotated[WorkerPrincipal, Depends(require_worker_auth)],
    settings: Settings = Depends(get_settings),
    session: AsyncSession = Depends(database_session),
    store: S3Store = Depends(get_s3_store),
) -> ArtifactUploadPrepareResponse:
    try:
        prepared = await WorkerService(settings, store).prepare_artifact_upload(
            session,
            worker_id=principal.worker_id,
            stage_id=stage_id,
            lease_token=payload.lease_token,
            sha256=payload.sha256,
            size_bytes=payload.size_bytes,
            media_type=payload.media_type,
            artifact_type=payload.artifact_type,
            schema_version=payload.schema_version,
        )
    except LeaseLost as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except ArtifactRejected as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return ArtifactUploadPrepareResponse(
        upload_required=prepared.upload_required,
        upload_url=prepared.upload_url,
        headers=prepared.headers,
        expires_at=prepared.expires_at,
    )


@router.post("/stages/{stage_id}/complete", response_model=StageStatusResponse)
async def complete_stage(
    stage_id: uuid.UUID,
    payload: StageCompleteRequest,
    principal: Annotated[WorkerPrincipal, Depends(require_worker_auth)],
    settings: Settings = Depends(get_settings),
    session: AsyncSession = Depends(database_session),
    store: S3Store = Depends(get_s3_store),
) -> StageStatusResponse:
    try:
        stage = await WorkerService(settings, store).complete(
            session,
            worker_id=principal.worker_id,
            stage_id=stage_id,
            lease_token=payload.lease_token,
            artifacts=[ArtifactCompletion(**artifact.model_dump()) for artifact in payload.artifacts],
            usage_events=[UsageCompletion(**event.model_dump()) for event in payload.usage_events],
        )
    except LeaseLost as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except ArtifactRejected as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return stage_response(stage)


@router.post("/stages/{stage_id}/fail", response_model=StageStatusResponse)
async def fail_stage(
    stage_id: uuid.UUID,
    payload: StageFailRequest,
    principal: Annotated[WorkerPrincipal, Depends(require_worker_auth)],
    settings: Settings = Depends(get_settings),
    session: AsyncSession = Depends(database_session),
    store: S3Store = Depends(get_s3_store),
) -> StageStatusResponse:
    try:
        stage = await WorkerService(settings, store).fail(
            session,
            worker_id=principal.worker_id,
            stage_id=stage_id,
            lease_token=payload.lease_token,
            error_message=payload.error_message,
            usage_events=[UsageCompletion(**event.model_dump()) for event in payload.usage_events],
        )
    except LeaseLost as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return stage_response(stage)
