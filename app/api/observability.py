from __future__ import annotations

import uuid
from collections import defaultdict
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, ConfigDict
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import joinedload

from ..persistence.models import (
    AIUsageEvent,
    Artifact,
    Asset,
    ClientProject,
    JobRequest,
    PipelineRun,
    ProviderConfig,
    StageRun,
    WebhookEvent,
    Worker,
)
from .auth import require_admin
from .v2 import database_session

router = APIRouter(
    prefix="/v2/admin",
    tags=["management"],
    dependencies=[Depends(require_admin)],
)


class CountByStatus(BaseModel):
    model_config = ConfigDict(extra="forbid")

    total: int
    queued: int = 0
    running: int = 0
    completed: int = 0
    failed: int = 0
    blocked: int = 0
    online: int = 0
    offline: int = 0


class OverviewResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    generated_at: datetime
    workers: CountByStatus
    jobs: CountByStatus
    active_stages: int
    clients: int
    enabled_providers: int
    webhook_events_attention: int
    usage_events_24h: int
    estimated_cost_usd_24h: Decimal


class ActiveWorkerStageResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    stage_run_id: uuid.UUID
    pipeline_run_id: uuid.UUID
    pipeline: str
    stage: str
    attempt: int
    progress: float | None
    lease_expires_at: datetime | None
    started_at: datetime | None


class WorkerResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    worker_id: uuid.UUID
    worker_key: str
    display_name: str
    status: str
    capabilities: dict[str, Any]
    registered_at: datetime
    last_seen_at: datetime
    active_stage: ActiveWorkerStageResponse | None


class JobListItemResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    job_id: uuid.UUID
    client_id: uuid.UUID
    client_name: str
    client_job_id: str | None
    asset_id: uuid.UUID
    source_filename: str
    pipeline_run_id: uuid.UUID
    pipeline: str
    pipeline_version: str
    status: str
    cache_hit: bool
    run_reused: bool
    stage_total: int
    stage_completed: int
    current_stage: str | None
    error_message: str | None
    created_at: datetime
    updated_at: datetime
    duration_seconds: float


class JobListResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    total: int
    items: list[JobListItemResponse]


class JobStageResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    stage_run_id: uuid.UUID
    name: str
    status: str
    required_capabilities: dict[str, Any]
    attempt: int
    max_attempts: int
    progress: float | None
    error_message: str | None
    worker_key: str | None
    worker_name: str | None
    lease_expires_at: datetime | None
    started_at: datetime | None
    completed_at: datetime | None


class JobArtifactResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    artifact_id: uuid.UUID
    artifact_type: str
    format: str
    schema_version: str
    sha256: str
    size_bytes: int
    media_type: str | None
    expires_at: datetime
    storage_state: str


class JobUsageResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    event_id: uuid.UUID
    stage_run_id: uuid.UUID
    stage_attempt: int
    provider: str
    model: str
    operation: str
    outcome: str
    estimated_cost_usd: Decimal | None
    latency_ms: int
    created_at: datetime


class JobWebhookResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    event_id: uuid.UUID
    event_type: str
    status: str
    attempt_count: int
    delivered_at: datetime | None
    abandoned_at: datetime | None
    last_error: str | None


class JobDetailResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    job: JobListItemResponse
    run_key: str
    schema_version: str
    options: dict[str, Any]
    processor_versions: dict[str, Any]
    run_expires_at: datetime
    source_sha256: str
    source_size_bytes: int
    source_media_type: str | None
    source_kind: str
    source_uri: str | None
    source_metadata: dict[str, Any]
    asset_expires_at: datetime
    stages: list[JobStageResponse]
    artifacts: list[JobArtifactResponse]
    usage: list[JobUsageResponse]
    webhooks: list[JobWebhookResponse]
    estimated_cost_usd: Decimal


async def _status_counts(session: AsyncSession, model) -> CountByStatus:
    rows = await session.execute(select(model.status, func.count()).group_by(model.status))
    values = {status: count for status, count in rows}
    status_values = {
        key: values.get(key, 0)
        for key in CountByStatus.model_fields
        if key != "total"
    }
    return CountByStatus(total=sum(values.values()), **status_values)


@router.get("/overview", response_model=OverviewResponse)
async def overview(session: AsyncSession = Depends(database_session)) -> OverviewResponse:
    cutoff = datetime.now(UTC) - timedelta(hours=24)
    workers = await _status_counts(session, Worker)
    jobs = await _status_counts(session, JobRequest)
    active_stages = await session.scalar(select(func.count()).select_from(StageRun).where(StageRun.status == "running"))
    clients = await session.scalar(select(func.count()).select_from(ClientProject))
    enabled_providers = await session.scalar(
        select(func.count()).select_from(ProviderConfig).where(ProviderConfig.enabled.is_(True))
    )
    webhook_events_attention = await session.scalar(
        select(func.count())
        .select_from(WebhookEvent)
        .where(
            WebhookEvent.status.in_({"retrying", "abandoned"}),
            WebhookEvent.created_at >= cutoff,
        )
    )
    usage_events_24h = await session.scalar(
        select(func.count()).select_from(AIUsageEvent).where(AIUsageEvent.created_at >= cutoff)
    )
    estimated_cost = await session.scalar(
        select(func.coalesce(func.sum(AIUsageEvent.estimated_cost_usd), 0)).where(
            AIUsageEvent.created_at >= cutoff
        )
    )
    return OverviewResponse(
        generated_at=datetime.now(UTC),
        workers=workers,
        jobs=jobs,
        active_stages=active_stages or 0,
        clients=clients or 0,
        enabled_providers=enabled_providers or 0,
        webhook_events_attention=webhook_events_attention or 0,
        usage_events_24h=usage_events_24h or 0,
        estimated_cost_usd_24h=estimated_cost or Decimal(0),
    )


@router.get("/workers", response_model=list[WorkerResponse])
async def list_workers(session: AsyncSession = Depends(database_session)) -> list[WorkerResponse]:
    workers = list(await session.scalars(select(Worker).order_by(Worker.display_name)))
    active_stages = list(
        await session.scalars(
            select(StageRun)
            .where(StageRun.status == "running", StageRun.lease_owner_id.is_not(None))
            .options(joinedload(StageRun.pipeline_run))
        )
    )
    active_by_worker = {stage.lease_owner_id: stage for stage in active_stages}
    result = []
    for worker in workers:
        stage = active_by_worker.get(worker.id)
        active = None
        if stage is not None:
            active = ActiveWorkerStageResponse(
                stage_run_id=stage.id,
                pipeline_run_id=stage.pipeline_run_id,
                pipeline=stage.pipeline_run.pipeline_name,
                stage=stage.stage_name,
                attempt=stage.attempt,
                progress=stage.progress,
                lease_expires_at=stage.lease_expires_at,
                started_at=stage.started_at,
            )
        result.append(
            WorkerResponse(
                worker_id=worker.id,
                worker_key=worker.worker_key,
                display_name=worker.display_name,
                status=worker.status,
                capabilities=dict(worker.capabilities),
                registered_at=worker.registered_at,
                last_seen_at=worker.last_seen_at,
                active_stage=active,
            )
        )
    return result


def _job_item(job: JobRequest, stages: list[StageRun], *, now: datetime) -> JobListItemResponse:
    priority = {"running": 0, "failed": 1, "queued": 2, "blocked": 3, "completed": 4}
    current = min(stages, key=lambda stage: priority.get(stage.status, 99), default=None)
    failure = next((stage.error_message for stage in stages if stage.error_message), None)
    end = job.updated_at if job.status in {"completed", "failed"} else now
    return JobListItemResponse(
        job_id=job.id,
        client_id=job.client_id,
        client_name=job.client.name,
        client_job_id=job.client_job_id,
        asset_id=job.asset_id,
        source_filename=job.asset.source_filename,
        pipeline_run_id=job.pipeline_run_id,
        pipeline=job.pipeline_run.pipeline_name,
        pipeline_version=job.pipeline_run.pipeline_version,
        status=job.status,
        cache_hit=job.cache_hit,
        run_reused=job.run_reused,
        stage_total=len(stages),
        stage_completed=sum(stage.status == "completed" for stage in stages),
        current_stage=current.stage_name if current and current.status != "completed" else None,
        error_message=failure,
        created_at=job.created_at,
        updated_at=job.updated_at,
        duration_seconds=max(0, (end - job.created_at).total_seconds()),
    )


@router.get("/jobs", response_model=JobListResponse)
async def list_jobs(
    job_status: Annotated[str | None, Query(alias="status", max_length=32)] = None,
    pipeline: Annotated[str | None, Query(max_length=128)] = None,
    client_id: uuid.UUID | None = None,
    limit: Annotated[int, Query(ge=1, le=500)] = 100,
    offset: Annotated[int, Query(ge=0)] = 0,
    session: AsyncSession = Depends(database_session),
) -> JobListResponse:
    filters = []
    if job_status:
        filters.append(JobRequest.status == job_status)
    if pipeline:
        filters.append(PipelineRun.pipeline_name == pipeline)
    if client_id:
        filters.append(JobRequest.client_id == client_id)

    base = select(JobRequest).join(PipelineRun)
    total = await session.scalar(
        select(func.count()).select_from(JobRequest).join(PipelineRun).where(*filters)
    )
    jobs = list(
        await session.scalars(
            base.where(*filters)
            .options(
                joinedload(JobRequest.client),
                joinedload(JobRequest.asset),
                joinedload(JobRequest.pipeline_run),
            )
            .order_by(JobRequest.created_at.desc())
            .offset(offset)
            .limit(limit)
        )
    )
    run_ids = {job.pipeline_run_id for job in jobs}
    stages_by_run: dict[uuid.UUID, list[StageRun]] = defaultdict(list)
    if run_ids:
        for stage in await session.scalars(
            select(StageRun).where(StageRun.pipeline_run_id.in_(run_ids)).order_by(StageRun.created_at)
        ):
            stages_by_run[stage.pipeline_run_id].append(stage)
    now = datetime.now(UTC)
    return JobListResponse(
        total=total or 0,
        items=[_job_item(job, stages_by_run[job.pipeline_run_id], now=now) for job in jobs],
    )


@router.get("/jobs/{job_id}", response_model=JobDetailResponse)
async def get_job_detail(
    job_id: uuid.UUID,
    session: AsyncSession = Depends(database_session),
) -> JobDetailResponse:
    job = await session.scalar(
        select(JobRequest)
        .where(JobRequest.id == job_id)
        .options(
            joinedload(JobRequest.client),
            joinedload(JobRequest.asset).joinedload(Asset.blob),
            joinedload(JobRequest.pipeline_run).selectinload(PipelineRun.stages).joinedload(StageRun.lease_owner),
            joinedload(JobRequest.pipeline_run)
            .selectinload(PipelineRun.artifacts)
            .joinedload(Artifact.blob),
        )
    )
    if job is None:
        raise HTTPException(status_code=404, detail="Job not found")

    run = job.pipeline_run
    stages = sorted(run.stages, key=lambda stage: stage.created_at)
    stage_ids = [stage.id for stage in stages]
    usage = []
    if stage_ids:
        usage = list(
            await session.scalars(
                select(AIUsageEvent)
                .where(AIUsageEvent.stage_run_id.in_(stage_ids))
                .order_by(AIUsageEvent.created_at)
            )
        )
    webhook_events = list(
        await session.scalars(
            select(WebhookEvent)
            .where(WebhookEvent.job_request_id == job.id)
            .order_by(WebhookEvent.created_at)
        )
    )
    now = datetime.now(UTC)
    return JobDetailResponse(
        job=_job_item(job, stages, now=now),
        run_key=run.run_key,
        schema_version=run.schema_version,
        options=dict(run.options),
        processor_versions=dict(run.processor_versions),
        run_expires_at=run.expires_at,
        source_sha256=job.asset.blob.sha256,
        source_size_bytes=job.asset.blob.size_bytes,
        source_media_type=job.asset.blob.media_type,
        source_kind=job.asset.source_kind,
        source_uri=job.asset.source_uri,
        source_metadata=dict(job.asset.source_metadata),
        asset_expires_at=job.asset.expires_at,
        stages=[
            JobStageResponse(
                stage_run_id=stage.id,
                name=stage.stage_name,
                status=stage.status,
                required_capabilities=dict(stage.required_capabilities),
                attempt=stage.attempt,
                max_attempts=stage.max_attempts,
                progress=stage.progress,
                error_message=stage.error_message,
                worker_key=stage.lease_owner.worker_key if stage.lease_owner else None,
                worker_name=stage.lease_owner.display_name if stage.lease_owner else None,
                lease_expires_at=stage.lease_expires_at,
                started_at=stage.started_at,
                completed_at=stage.completed_at,
            )
            for stage in stages
        ],
        artifacts=[
            JobArtifactResponse(
                artifact_id=artifact.id,
                artifact_type=artifact.artifact_type,
                format=artifact.format,
                schema_version=artifact.schema_version,
                sha256=artifact.blob.sha256,
                size_bytes=artifact.blob.size_bytes,
                media_type=artifact.blob.media_type,
                expires_at=artifact.expires_at,
                storage_state=artifact.blob.state,
            )
            for artifact in sorted(run.artifacts, key=lambda item: item.artifact_type)
        ],
        usage=[
            JobUsageResponse(
                event_id=event.id,
                stage_run_id=event.stage_run_id,
                stage_attempt=event.stage_attempt,
                provider=event.provider,
                model=event.model,
                operation=event.operation,
                outcome=event.outcome,
                estimated_cost_usd=event.estimated_cost_usd,
                latency_ms=event.latency_ms,
                created_at=event.created_at,
            )
            for event in usage
        ],
        webhooks=[
            JobWebhookResponse(
                event_id=event.id,
                event_type=event.event_type,
                status=event.status,
                attempt_count=event.attempt_count,
                delivered_at=event.delivered_at,
                abandoned_at=event.abandoned_at,
                last_error=event.last_error,
            )
            for event in webhook_events
        ],
        estimated_cost_usd=sum(
            (event.estimated_cost_usd or Decimal(0) for event in usage),
            start=Decimal(0),
        ),
    )
