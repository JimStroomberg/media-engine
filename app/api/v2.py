from __future__ import annotations

import json
import uuid
from datetime import UTC, datetime
from typing import Annotated, Any, Literal

from fastapi import APIRouter, Depends, File, Form, Header, HTTPException, Request, Response, UploadFile, status
from pydantic import BaseModel, ConfigDict, Field, ValidationError
from sqlalchemy.ext.asyncio import AsyncSession

from ..config import Settings, get_settings
from ..domain.pipelines import PipelineDefinition, UnsupportedPipeline, get_pipeline, list_pipelines
from ..persistence.database import get_session_factory
from ..persistence.models import Artifact, Asset, JobRequest
from ..security import CredentialCipher
from ..services.access import ClientPrincipal
from ..services.assets import AssetIngestResult, AssetService, AssetUploadTooLarge
from ..services.jobs import AssetNotFound, AssetUnavailable, IdempotencyConflict, JobService
from ..services.providers import ProviderConfigurationService, ProviderUnavailable
from ..services.webhooks import WebhookEndpointNotFound, WebhookEndpointService
from ..storage.s3 import S3Store
from .auth import get_credential_cipher, require_client_scope

router = APIRouter(prefix="/v2", tags=["platform-v2"])


class ErrorResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    detail: str


class AssetResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    asset_id: uuid.UUID
    sha256: str
    size_bytes: int
    media_type: str | None
    source_filename: str
    source_kind: str
    source_uri: str | None
    source_metadata: dict[str, Any]
    created_at: datetime
    expires_at: datetime
    storage_state: str
    duplicate_content: bool = False


class WebhookSelection(BaseModel):
    model_config = ConfigDict(extra="forbid")

    endpoint_id: uuid.UUID


class JobCreateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    asset_id: uuid.UUID
    pipeline: str = Field(min_length=1, max_length=128)
    pipeline_version: str | None = Field(default=None, min_length=1, max_length=64)
    options: dict[str, Any] = Field(default_factory=dict)
    client_job_id: str | None = Field(default=None, min_length=1, max_length=255)
    webhook: WebhookSelection | Literal[False] | None = None


class JobResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    job_id: uuid.UUID
    asset_id: uuid.UUID
    pipeline_run_id: uuid.UUID
    run_key: str
    pipeline: str
    pipeline_version: str
    status: str
    cache_hit: bool
    run_reused: bool
    idempotent_replay: bool = False
    client_job_id: str | None
    webhook_endpoint_id: uuid.UUID | None
    created_at: datetime
    updated_at: datetime
    expires_at: datetime


class ArtifactResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    artifact_id: uuid.UUID
    artifact_type: str
    format: str
    schema_version: str
    sha256: str
    size_bytes: int
    media_type: str | None
    expires_at: datetime
    available: bool
    download_url: str | None


class PipelineArtifactContractResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    artifact_type: str
    required: bool
    schema_version: str


class PipelineStageContractResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str
    processor: str
    depends_on: list[str]
    required_capabilities: dict[str, list[str]]
    outputs: list[PipelineArtifactContractResponse]


class PipelineResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str
    version: str
    schema_version: str
    processor_versions: dict[str, str]
    options_schema: dict[str, Any]
    required_artifacts: list[str]
    stages: list[PipelineStageContractResponse]


class ManifestSourceResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    asset_id: uuid.UUID
    sha256: str
    size_bytes: int
    media_type: str | None
    source_filename: str
    source_kind: str
    source_uri: str | None
    source_metadata: dict[str, Any]
    created_at: datetime
    expires_at: datetime
    available: bool


class ManifestStageResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    stage_run_id: uuid.UUID
    name: str
    processor: str
    status: str
    depends_on: list[str]
    required_capabilities: dict[str, Any]
    output_contract: list[PipelineArtifactContractResponse]
    artifact_types: list[str]
    attempt: int
    max_attempts: int
    progress: float | None
    error_message: str | None
    started_at: datetime | None
    completed_at: datetime | None


class ManifestArtifactResponse(ArtifactResponse):
    producer_stage_run_id: uuid.UUID
    producer_stage: str


class JobManifestResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    manifest_version: str = "1"
    generated_at: datetime
    job: JobResponse
    source: ManifestSourceResponse
    options: dict[str, Any]
    processor_versions: dict[str, Any]
    stages: list[ManifestStageResponse]
    artifacts: list[ManifestArtifactResponse]


async def database_session(settings: Settings = Depends(get_settings)):
    if not settings.platform_v2_enabled:
        raise HTTPException(status_code=503, detail="Platform v2 requires PostgreSQL and S3 configuration")
    factory = get_session_factory()
    async with factory() as session:
        yield session


def get_s3_store(request: Request) -> S3Store:
    store = getattr(request.app.state, "s3_store", None)
    if store is None:
        raise HTTPException(status_code=503, detail="Platform S3 storage is not ready")
    return store


def parse_source_metadata(raw: str | None) -> dict[str, Any]:
    if raw is None or raw.strip() == "":
        return {}
    try:
        value = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise HTTPException(status_code=422, detail="source_metadata must be valid JSON") from exc
    if not isinstance(value, dict):
        raise HTTPException(status_code=422, detail="source_metadata must be a JSON object")
    return value


def response_from_asset(asset: Asset, *, duplicate_content: bool = False) -> AssetResponse:
    return AssetResponse(
        asset_id=asset.id,
        sha256=asset.blob.sha256,
        size_bytes=asset.blob.size_bytes,
        media_type=asset.blob.media_type,
        source_filename=asset.source_filename,
        source_kind=asset.source_kind,
        source_uri=asset.source_uri,
        source_metadata=asset.source_metadata,
        created_at=asset.created_at,
        expires_at=asset.expires_at,
        storage_state=asset.blob.state,
        duplicate_content=duplicate_content,
    )


def response_from_job(job: JobRequest, *, idempotent_replay: bool = False) -> JobResponse:
    return JobResponse(
        job_id=job.id,
        asset_id=job.asset_id,
        pipeline_run_id=job.pipeline_run_id,
        run_key=job.pipeline_run.run_key,
        pipeline=job.pipeline_run.pipeline_name,
        pipeline_version=job.pipeline_run.pipeline_version,
        status=job.status,
        cache_hit=job.cache_hit,
        run_reused=job.run_reused,
        idempotent_replay=idempotent_replay,
        client_job_id=job.client_job_id,
        webhook_endpoint_id=job.webhook_endpoint_id,
        created_at=job.created_at,
        updated_at=job.updated_at,
        expires_at=job.pipeline_run.expires_at,
    )


def response_from_pipeline(pipeline: PipelineDefinition) -> PipelineResponse:
    return PipelineResponse(
        name=pipeline.name,
        version=pipeline.version,
        schema_version=pipeline.schema_version,
        processor_versions=dict(pipeline.processor_versions),
        options_schema=pipeline.options_model.model_json_schema(),
        required_artifacts=list(pipeline.required_artifacts),
        stages=[
            PipelineStageContractResponse(
                name=stage.name,
                processor=stage.processor,
                depends_on=list(stage.depends_on),
                required_capabilities={key: list(values) for key, values in stage.required_capabilities.items()},
                outputs=[
                    PipelineArtifactContractResponse(
                        artifact_type=output.artifact_type,
                        required=output.required,
                        schema_version=output.schema_version,
                    )
                    for output in stage.outputs
                ],
            )
            for stage in pipeline.stages
        ],
    )


@router.get(
    "/pipelines",
    response_model=list[PipelineResponse],
    summary="List pipeline contracts",
)
async def get_pipelines() -> list[PipelineResponse]:
    """List server-owned, versioned processing contracts."""

    return [response_from_pipeline(pipeline) for pipeline in list_pipelines()]


@router.get(
    "/pipelines/{pipeline_name}",
    response_model=PipelineResponse,
    summary="Get a pipeline contract",
    responses={404: {"model": ErrorResponse, "description": "Pipeline not found"}},
)
async def get_pipeline_contract(pipeline_name: str, version: str | None = None) -> PipelineResponse:
    """Describe the requested version, or the latest version, of one processing contract."""

    try:
        return response_from_pipeline(get_pipeline(pipeline_name, version))
    except UnsupportedPipeline as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.post(
    "/assets",
    response_model=AssetResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Ingest a media asset",
    responses={
        413: {"model": ErrorResponse, "description": "Upload exceeds the configured limit"},
        422: {"model": ErrorResponse, "description": "Invalid media or source metadata"},
        503: {"model": ErrorResponse, "description": "PostgreSQL or S3 is unavailable"},
    },
)
async def create_asset(
    file: Annotated[UploadFile, File(description="Media file to ingest")],
    source_metadata: Annotated[str | None, Form()] = None,
    source_kind: Annotated[str, Form(min_length=1, max_length=64)] = "upload",
    source_uri: Annotated[str | None, Form(max_length=4096)] = None,
    principal: ClientPrincipal = Depends(require_client_scope("assets:write")),
    settings: Settings = Depends(get_settings),
    session: AsyncSession = Depends(database_session),
    store: S3Store = Depends(get_s3_store),
) -> AssetResponse:
    service = AssetService(settings, store)
    try:
        result: AssetIngestResult = await service.ingest_upload(
            session,
            file,
            client_id=principal.client_id,
            source_metadata=parse_source_metadata(source_metadata),
            source_kind=source_kind,
            source_uri=source_uri,
        )
    except AssetUploadTooLarge as exc:
        raise HTTPException(status_code=413, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return response_from_asset(result.asset, duplicate_content=result.duplicate_content)


@router.get(
    "/assets/{asset_id}",
    response_model=AssetResponse,
    summary="Get an asset",
    responses={
        404: {"model": ErrorResponse, "description": "Asset not found"},
        503: {"model": ErrorResponse, "description": "PostgreSQL or S3 is unavailable"},
    },
)
async def get_asset(
    asset_id: uuid.UUID,
    principal: ClientPrincipal = Depends(require_client_scope("assets:read")),
    settings: Settings = Depends(get_settings),
    session: AsyncSession = Depends(database_session),
    store: S3Store = Depends(get_s3_store),
) -> AssetResponse:
    service = AssetService(settings, store)
    asset = await service.get_asset(session, asset_id, client_id=principal.client_id)
    if asset is None:
        raise HTTPException(status_code=404, detail="Asset not found")
    return response_from_asset(asset)


@router.post(
    "/jobs",
    response_model=JobResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Submit a durable pipeline job",
    responses={
        200: {"model": JobResponse, "description": "Idempotent replay of an existing request"},
        404: {"model": ErrorResponse, "description": "Asset not found"},
        409: {"model": ErrorResponse, "description": "Idempotency key conflicts with another request"},
        410: {"model": ErrorResponse, "description": "Asset bytes have expired or disappeared"},
        422: {"model": ErrorResponse, "description": "Invalid pipeline or options"},
        503: {"model": ErrorResponse, "description": "PostgreSQL or S3 is unavailable"},
    },
)
async def create_job(
    payload: JobCreateRequest,
    response: Response,
    idempotency_key: Annotated[
        str | None,
        Header(alias="Idempotency-Key", min_length=1, max_length=255),
    ] = None,
    principal: ClientPrincipal = Depends(require_client_scope("jobs:write")),
    settings: Settings = Depends(get_settings),
    cipher: CredentialCipher = Depends(get_credential_cipher),
    session: AsyncSession = Depends(database_session),
    store: S3Store = Depends(get_s3_store),
) -> JobResponse:
    try:
        pipeline = get_pipeline(payload.pipeline, payload.pipeline_version)
        raw_options = payload.options
        provider_service = ProviderConfigurationService(settings, cipher)
        if pipeline.name == "understand" and pipeline.version == "2":
            raw_options = await provider_service.resolve_understand_options(session, raw_options)
        elif pipeline.name == "understand":
            await provider_service.runtime_connection(session, "openai")
        options = pipeline.normalize_options(raw_options)
        webhook_selection: uuid.UUID | bool | None
        if isinstance(payload.webhook, WebhookSelection):
            webhook_selection = payload.webhook.endpoint_id
        else:
            webhook_selection = payload.webhook
        webhook_endpoint_id = await WebhookEndpointService(settings, cipher).resolve_for_job(
            session,
            client_id=principal.client_id,
            selection=webhook_selection,
        )
        result = await JobService(settings, store).submit(
            session,
            client_id=principal.client_id,
            asset_id=payload.asset_id,
            pipeline=pipeline,
            options=options,
            client_job_id=payload.client_job_id,
            callback_url=None,
            webhook_endpoint_id=webhook_endpoint_id,
            idempotency_key=idempotency_key,
        )
    except (UnsupportedPipeline, ValidationError) as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except ProviderUnavailable as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    except AssetNotFound as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except AssetUnavailable as exc:
        raise HTTPException(status_code=410, detail=str(exc)) from exc
    except IdempotencyConflict as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except WebhookEndpointNotFound as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc

    if result.idempotent_replay:
        response.status_code = status.HTTP_200_OK
    return response_from_job(result.job, idempotent_replay=result.idempotent_replay)


@router.get(
    "/jobs/{job_id}",
    response_model=JobResponse,
    summary="Get durable job status",
    responses={404: {"model": ErrorResponse, "description": "Job not found"}},
)
async def get_job(
    job_id: uuid.UUID,
    principal: ClientPrincipal = Depends(require_client_scope("jobs:read")),
    settings: Settings = Depends(get_settings),
    session: AsyncSession = Depends(database_session),
    store: S3Store = Depends(get_s3_store),
) -> JobResponse:
    job = await JobService(settings, store).get(
        session,
        job_id,
        client_id=principal.client_id,
    )
    if job is None:
        raise HTTPException(status_code=404, detail="Job not found")
    return response_from_job(job)


@router.get(
    "/jobs/{job_id}/artifacts",
    response_model=list[ArtifactResponse],
    summary="List job artifacts",
    responses={404: {"model": ErrorResponse, "description": "Job not found"}},
)
async def get_job_artifacts(
    job_id: uuid.UUID,
    principal: ClientPrincipal = Depends(require_client_scope("jobs:read")),
    settings: Settings = Depends(get_settings),
    session: AsyncSession = Depends(database_session),
    store: S3Store = Depends(get_s3_store),
) -> list[ArtifactResponse]:
    artifacts = await JobService(settings, store).list_artifacts(
        session,
        job_id,
        client_id=principal.client_id,
    )
    if artifacts is None:
        raise HTTPException(status_code=404, detail="Job not found")

    now = datetime.now(UTC)
    response: list[ArtifactResponse] = []
    for artifact in artifacts:
        available = (
            artifact.expires_at > now
            and artifact.blob.state == "available"
            and artifact.blob.expires_at > now
            and await store.exists(artifact.blob.object_key)
        )
        download_url = None
        if available:
            filename = f"{artifact.artifact_type}.{artifact.format}"
            download_url = store.create_download_url(
                artifact.blob.object_key,
                expires_in=settings.artifact_url_expiry_seconds,
                filename=filename,
            )
        response.append(artifact_response(artifact, available=available, download_url=download_url))
    return response


@router.get(
    "/jobs/{job_id}/manifest",
    response_model=JobManifestResponse,
    summary="Get the complete job manifest",
    responses={404: {"model": ErrorResponse, "description": "Job not found"}},
)
async def get_job_manifest(
    job_id: uuid.UUID,
    principal: ClientPrincipal = Depends(require_client_scope("jobs:read")),
    settings: Settings = Depends(get_settings),
    session: AsyncSession = Depends(database_session),
    store: S3Store = Depends(get_s3_store),
) -> JobManifestResponse:
    """Return source metadata, stage provenance, and all outputs for one job."""

    data = await JobService(settings, store).manifest(
        session,
        job_id,
        client_id=principal.client_id,
    )
    if data is None:
        raise HTTPException(status_code=404, detail="Job not found")

    now = datetime.now(UTC)
    pipeline = get_pipeline(data.job.pipeline_run.pipeline_name, data.job.pipeline_run.pipeline_version)
    stage_by_id = {stage.id: stage for stage in data.stages}
    stage_name_by_id = {stage.id: stage.stage_name for stage in data.stages}
    dependency_names: dict[uuid.UUID, list[str]] = {stage.id: [] for stage in data.stages}
    for dependency in data.dependencies:
        dependency_names[dependency.stage_run_id].append(stage_name_by_id[dependency.depends_on_stage_run_id])

    artifact_responses: list[ManifestArtifactResponse] = []
    artifacts_by_stage: dict[uuid.UUID, list[str]] = {stage.id: [] for stage in data.stages}
    for artifact in data.artifacts:
        available = (
            artifact.expires_at > now
            and artifact.blob.state == "available"
            and artifact.blob.expires_at > now
            and await store.exists(artifact.blob.object_key)
        )
        download_url = None
        if available:
            download_url = store.create_download_url(
                artifact.blob.object_key,
                expires_in=settings.artifact_url_expiry_seconds,
                filename=f"{artifact.artifact_type}.{artifact.format}",
            )
        artifacts_by_stage[artifact.producer_stage_run_id].append(artifact.artifact_type)
        base = artifact_response(artifact, available=available, download_url=download_url)
        artifact_responses.append(
            ManifestArtifactResponse(
                **base.model_dump(),
                producer_stage_run_id=artifact.producer_stage_run_id,
                producer_stage=stage_by_id[artifact.producer_stage_run_id].stage_name,
            )
        )

    source_blob = data.job.asset.blob
    source_available = (
        data.job.asset.expires_at > now
        and source_blob.state == "available"
        and source_blob.expires_at > now
        and await store.exists(source_blob.object_key)
    )
    stages = []
    stages_by_name = {stage.stage_name: stage for stage in data.stages}
    for stage_contract in pipeline.stages:
        stage = stages_by_name[stage_contract.name]
        stages.append(
            ManifestStageResponse(
                stage_run_id=stage.id,
                name=stage.stage_name,
                processor=stage_contract.processor,
                status=stage.status,
                depends_on=dependency_names[stage.id],
                required_capabilities=stage.required_capabilities,
                output_contract=[
                    PipelineArtifactContractResponse(
                        artifact_type=output.artifact_type,
                        required=output.required,
                        schema_version=output.schema_version,
                    )
                    for output in stage_contract.outputs
                ],
                artifact_types=sorted(artifacts_by_stage[stage.id]),
                attempt=stage.attempt,
                max_attempts=stage.max_attempts,
                progress=stage.progress,
                error_message=stage.error_message,
                started_at=stage.started_at,
                completed_at=stage.completed_at,
            )
        )

    return JobManifestResponse(
        generated_at=now,
        job=response_from_job(data.job),
        source=ManifestSourceResponse(
            asset_id=data.job.asset.id,
            sha256=source_blob.sha256,
            size_bytes=source_blob.size_bytes,
            media_type=source_blob.media_type,
            source_filename=data.job.asset.source_filename,
            source_kind=data.job.asset.source_kind,
            source_uri=data.job.asset.source_uri,
            source_metadata=data.job.asset.source_metadata,
            created_at=data.job.asset.created_at,
            expires_at=data.job.asset.expires_at,
            available=source_available,
        ),
        options=data.job.pipeline_run.options,
        processor_versions=data.job.pipeline_run.processor_versions,
        stages=stages,
        artifacts=artifact_responses,
    )


def artifact_response(artifact: Artifact, *, available: bool, download_url: str | None) -> ArtifactResponse:
    return ArtifactResponse(
        artifact_id=artifact.id,
        artifact_type=artifact.artifact_type,
        format=artifact.format,
        schema_version=artifact.schema_version,
        sha256=artifact.blob.sha256,
        size_bytes=artifact.blob.size_bytes,
        media_type=artifact.blob.media_type,
        expires_at=artifact.expires_at,
        available=available,
        download_url=download_url,
    )
