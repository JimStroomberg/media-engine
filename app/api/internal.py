from __future__ import annotations

import secrets
import uuid
from datetime import datetime
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Header, HTTPException, Response, status
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy.ext.asyncio import AsyncSession

from ..config import Settings, get_settings
from ..domain.pipelines import get_pipeline
from ..persistence.models import Artifact, StageRun, Worker
from ..security import CredentialCipher
from ..services.providers import ProviderConfigurationService, RuntimeProviderConnection
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


def require_worker_auth(
    authorization: Annotated[str | None, Header()] = None,
    settings: Settings = Depends(get_settings),
) -> None:
    if not settings.worker_api_token:
        raise HTTPException(status_code=503, detail="Internal worker API is not configured")
    expected = f"Bearer {settings.worker_api_token}"
    if authorization is None or not secrets.compare_digest(authorization, expected):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid worker credentials",
            headers={"WWW-Authenticate": "Bearer"},
        )


router = APIRouter(
    prefix="/v2/internal",
    tags=["internal-workers"],
    dependencies=[Depends(require_worker_auth)],
)


class WorkerRegisterRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    worker_key: str = Field(min_length=1, max_length=255)
    display_name: str = Field(min_length=1, max_length=255)
    capabilities: dict[str, list[str]]


class WorkerResponse(BaseModel):
    worker_id: uuid.UUID
    worker_key: str
    display_name: str
    capabilities: dict[str, Any]
    status: str
    registered_at: datetime
    last_seen_at: datetime


class StageClaimRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    worker_id: uuid.UUID


class StageSourceResponse(BaseModel):
    bucket: str
    object_key: str
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

    worker_id: uuid.UUID
    lease_token: uuid.UUID
    progress: float | None = Field(default=None, ge=0.0, le=1.0)


class ArtifactCompletionRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    size_bytes: int = Field(gt=0)
    media_type: str | None = Field(default=None, max_length=255)
    object_key: str = Field(min_length=1)
    etag: str | None = Field(default=None, max_length=255)
    artifact_type: str = Field(min_length=1, max_length=128)
    format: str = Field(min_length=1, max_length=64)
    schema_version: str = Field(default="1", min_length=1, max_length=64)


class StageCompleteRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    worker_id: uuid.UUID
    lease_token: uuid.UUID
    artifacts: list[ArtifactCompletionRequest] = Field(min_length=1, max_length=32)
    usage_events: list[ProviderUsageRequest] = Field(default_factory=list, max_length=100)


class StageFailRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    worker_id: uuid.UUID
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
        capabilities=worker.capabilities,
        status=worker.status,
        registered_at=worker.registered_at,
        last_seen_at=worker.last_seen_at,
    )


def claim_response(
    stage: StageRun,
    inputs: list[Artifact],
    provider_connection: RuntimeProviderConnection | None,
) -> StageClaimResponse:
    run = stage.pipeline_run
    source = run.source_blob
    pipeline = get_pipeline(run.pipeline_name, run.pipeline_version)
    stage_definition = pipeline.stage(stage.stage_name)
    if stage.lease_token is None or stage.lease_expires_at is None:
        raise RuntimeError("Claimed stage has no active lease")
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
            bucket=source.bucket,
            object_key=source.object_key,
            sha256=source.sha256,
            size_bytes=source.size_bytes,
            media_type=source.media_type,
        ),
        inputs=[
            StageArtifactInputResponse(
                bucket=artifact.blob.bucket,
                object_key=artifact.blob.object_key,
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
    settings: Settings = Depends(get_settings),
    session: AsyncSession = Depends(database_session),
    store: S3Store = Depends(get_s3_store),
) -> WorkerResponse:
    worker = await WorkerService(settings, store).register(
        session,
        worker_key=payload.worker_key,
        display_name=payload.display_name,
        capabilities=payload.capabilities,
    )
    return worker_response(worker)


@router.post("/stages/claim", response_model=StageClaimResponse)
async def claim_stage(
    payload: StageClaimRequest,
    settings: Settings = Depends(get_settings),
    cipher: CredentialCipher = Depends(get_credential_cipher),
    session: AsyncSession = Depends(database_session),
    store: S3Store = Depends(get_s3_store),
):
    try:
        service = WorkerService(settings, store)
        stage = await service.claim(session, worker_id=payload.worker_id)
    except WorkerNotFound as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    if stage is None:
        return Response(status_code=status.HTTP_204_NO_CONTENT)
    inputs = await service.get_stage_inputs(session, stage.id)
    requested_providers = stage.required_capabilities.get("providers", [])
    provider_connection = None
    if requested_providers:
        provider_connection = await ProviderConfigurationService(
            settings,
            cipher,
        ).runtime_connection(session, str(requested_providers[0]))
    return claim_response(stage, inputs, provider_connection)


@router.post("/stages/{stage_id}/heartbeat", response_model=StageStatusResponse)
async def heartbeat_stage(
    stage_id: uuid.UUID,
    payload: StageHeartbeatRequest,
    settings: Settings = Depends(get_settings),
    session: AsyncSession = Depends(database_session),
    store: S3Store = Depends(get_s3_store),
) -> StageStatusResponse:
    try:
        stage = await WorkerService(settings, store).heartbeat(
            session,
            worker_id=payload.worker_id,
            stage_id=stage_id,
            lease_token=payload.lease_token,
            progress=payload.progress,
        )
    except LeaseLost as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return stage_response(stage)


@router.post("/stages/{stage_id}/complete", response_model=StageStatusResponse)
async def complete_stage(
    stage_id: uuid.UUID,
    payload: StageCompleteRequest,
    settings: Settings = Depends(get_settings),
    session: AsyncSession = Depends(database_session),
    store: S3Store = Depends(get_s3_store),
) -> StageStatusResponse:
    try:
        stage = await WorkerService(settings, store).complete(
            session,
            worker_id=payload.worker_id,
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
    settings: Settings = Depends(get_settings),
    session: AsyncSession = Depends(database_session),
    store: S3Store = Depends(get_s3_store),
) -> StageStatusResponse:
    try:
        stage = await WorkerService(settings, store).fail(
            session,
            worker_id=payload.worker_id,
            stage_id=stage_id,
            lease_token=payload.lease_token,
            error_message=payload.error_message,
            usage_events=[UsageCompletion(**event.model_dump()) for event in payload.usage_events],
        )
    except LeaseLost as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return stage_response(stage)
