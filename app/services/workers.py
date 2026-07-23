from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal, InvalidOperation
from typing import Any

from sqlalchemy import func, select, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import joinedload

from ..config import Settings
from ..domain.content import content_addressed_key
from ..domain.pipelines import PipelineDefinition, get_pipeline
from ..persistence.models import (
    AIUsageEvent,
    Artifact,
    Blob,
    JobRequest,
    PipelineRun,
    ProviderConfig,
    StageDependency,
    StageRun,
    Worker,
)
from ..storage.s3 import S3Store
from .webhooks import enqueue_job_webhook_event


class WorkerNotFound(RuntimeError):
    pass


class LeaseLost(RuntimeError):
    pass


class ArtifactRejected(RuntimeError):
    pass


@dataclass(frozen=True)
class ArtifactCompletion:
    sha256: str
    size_bytes: int
    media_type: str | None
    object_key: str
    etag: str | None
    artifact_type: str
    format: str
    schema_version: str


@dataclass(frozen=True)
class UsageCompletion:
    event_id: uuid.UUID
    provider: str
    model: str
    operation: str
    outcome: str
    usage: dict[str, Any]
    latency_ms: int
    started_at: datetime
    completed_at: datetime


def capabilities_satisfy(actual: dict[str, Any], required: dict[str, Any]) -> bool:
    """Return whether a worker advertises every required capability value."""

    for key, required_values in required.items():
        actual_values = actual.get(key, [])
        if not isinstance(required_values, list) or not isinstance(actual_values, list):
            return False
        if not set(required_values).issubset(actual_values):
            return False
    return True


class WorkerService:
    def __init__(self, settings: Settings, store: S3Store) -> None:
        self.settings = settings
        self.store = store

    async def register(
        self,
        session: AsyncSession,
        *,
        worker_key: str,
        display_name: str,
        capabilities: dict[str, Any],
    ) -> Worker:
        now = datetime.now(UTC)
        statement = (
            insert(Worker)
            .values(
                id=uuid.uuid4(),
                worker_key=worker_key,
                display_name=display_name,
                capabilities=capabilities,
                status="online",
                last_seen_at=now,
            )
            .on_conflict_do_update(
                index_elements=[Worker.worker_key],
                set_={
                    "display_name": display_name,
                    "capabilities": capabilities,
                    "status": "online",
                    "last_seen_at": now,
                    "updated_at": now,
                },
            )
            .returning(Worker.id)
        )
        worker_id = (await session.execute(statement)).scalar_one()
        await session.commit()
        worker = await session.get(Worker, worker_id)
        if worker is None:
            raise RuntimeError("Worker disappeared after registration")
        return worker

    async def claim(self, session: AsyncSession, *, worker_id: uuid.UUID) -> StageRun | None:
        now = datetime.now(UTC)
        worker = await session.scalar(select(Worker).where(Worker.id == worker_id).with_for_update())
        if worker is None:
            raise WorkerNotFound(f"Worker {worker_id} was not found")
        worker.status = "online"
        worker.last_seen_at = now

        candidates = await session.scalars(
            select(StageRun)
            .where(StageRun.status == "queued")
            .order_by(StageRun.priority.desc(), StageRun.created_at)
            .limit(50)
            .with_for_update(skip_locked=True)
        )
        enabled_providers = set(
            await session.scalars(
                select(ProviderConfig.provider).where(ProviderConfig.enabled.is_(True))
            )
        )
        stage = next(
            (
                candidate
                for candidate in candidates
                if capabilities_satisfy(worker.capabilities, candidate.required_capabilities)
                and set(candidate.required_capabilities.get("providers", [])).issubset(
                    enabled_providers
                )
            ),
            None,
        )
        if stage is None:
            await session.commit()
            return None

        lease_token = uuid.uuid4()
        stage.status = "running"
        stage.attempt += 1
        stage.lease_owner_id = worker.id
        stage.lease_token = lease_token
        stage.lease_expires_at = now + timedelta(seconds=self.settings.worker_lease_seconds)
        stage.heartbeat_at = now
        stage.progress = 0.0
        stage.error_message = None
        stage.started_at = stage.started_at or now

        run = await session.get(PipelineRun, stage.pipeline_run_id, with_for_update=True)
        if run is None:
            raise RuntimeError("Pipeline run disappeared while claiming a stage")
        run.status = "running"
        await session.execute(
            update(JobRequest)
            .where(JobRequest.pipeline_run_id == run.id, JobRequest.status == "queued")
            .values(status="running", updated_at=now)
        )
        await session.commit()
        claimed = await self.get_stage(session, stage.id)
        if claimed is None:
            raise RuntimeError("Stage disappeared after claim")
        return claimed

    async def heartbeat(
        self,
        session: AsyncSession,
        *,
        worker_id: uuid.UUID,
        stage_id: uuid.UUID,
        lease_token: uuid.UUID,
        progress: float | None,
    ) -> StageRun:
        now = datetime.now(UTC)
        stage = await self._locked_stage(session, stage_id)
        self._validate_active_lease(stage, worker_id=worker_id, lease_token=lease_token, now=now)
        stage.heartbeat_at = now
        stage.lease_expires_at = now + timedelta(seconds=self.settings.worker_lease_seconds)
        if progress is not None:
            stage.progress = progress
        await session.execute(
            update(Worker).where(Worker.id == worker_id).values(last_seen_at=now, status="online", updated_at=now)
        )
        await session.commit()
        refreshed = await self.get_stage(session, stage_id)
        if refreshed is None:
            raise RuntimeError("Stage disappeared after heartbeat")
        return refreshed

    async def complete(
        self,
        session: AsyncSession,
        *,
        worker_id: uuid.UUID,
        stage_id: uuid.UUID,
        lease_token: uuid.UUID,
        artifacts: list[ArtifactCompletion],
        usage_events: list[UsageCompletion],
    ) -> StageRun:
        now = datetime.now(UTC)
        expires_at = now + timedelta(hours=self.settings.pipeline_retention_hours)
        stage = await self._locked_stage(session, stage_id)
        if stage.status == "completed":
            self._validate_lease_identity(stage, worker_id=worker_id, lease_token=lease_token)
            await session.rollback()
            completed = await self.get_stage(session, stage_id)
            if completed is None:
                raise RuntimeError("Completed stage disappeared")
            return completed
        self._validate_active_lease(stage, worker_id=worker_id, lease_token=lease_token, now=now)

        run = await session.get(PipelineRun, stage.pipeline_run_id, with_for_update=True)
        if run is None:
            raise RuntimeError("Pipeline run disappeared while completing a stage")
        pipeline = get_pipeline(run.pipeline_name, run.pipeline_version)
        self._validate_artifact_contract(pipeline, stage, artifacts, run.schema_version)

        for artifact in sorted(artifacts, key=lambda item: item.sha256):
            expected_key = content_addressed_key(artifact.sha256)
            if artifact.object_key != expected_key:
                raise ArtifactRejected("Reported artifact is not available at its content-addressed S3 key")

            # Retention takes the same lock before deleting an existing object.
            await session.scalar(select(Blob).where(Blob.sha256 == artifact.sha256).with_for_update())
            if not await self.store.exists(artifact.object_key):
                raise ArtifactRejected("Reported artifact is not available at its content-addressed S3 key")

            blob_statement = (
                insert(Blob)
                .values(
                    id=uuid.uuid4(),
                    sha256=artifact.sha256,
                    size_bytes=artifact.size_bytes,
                    media_type=artifact.media_type,
                    bucket=self.store.bucket,
                    object_key=artifact.object_key,
                    etag=artifact.etag,
                    state="available",
                    expires_at=expires_at,
                    expired_at=None,
                )
                .on_conflict_do_update(
                    index_elements=[Blob.sha256],
                    set_={
                        "size_bytes": artifact.size_bytes,
                        "media_type": artifact.media_type,
                        "bucket": self.store.bucket,
                        "object_key": artifact.object_key,
                        "etag": artifact.etag,
                        "state": "available",
                        "expires_at": func.greatest(Blob.expires_at, expires_at),
                        "expired_at": None,
                    },
                )
                .returning(Blob.id)
            )
            artifact_blob_id = (await session.execute(blob_statement)).scalar_one()
            artifact_statement = (
                insert(Artifact)
                .values(
                    id=uuid.uuid4(),
                    pipeline_run_id=stage.pipeline_run_id,
                    producer_stage_run_id=stage.id,
                    blob_id=artifact_blob_id,
                    artifact_type=artifact.artifact_type,
                    format=artifact.format,
                    schema_version=artifact.schema_version,
                    expires_at=expires_at,
                )
                .on_conflict_do_update(
                    constraint="uq_artifact_run_type",
                    set_={
                        "producer_stage_run_id": stage.id,
                        "blob_id": artifact_blob_id,
                        "format": artifact.format,
                        "schema_version": artifact.schema_version,
                        "expires_at": expires_at,
                    },
                )
            )
            await session.execute(artifact_statement)

        stage.status = "completed"
        stage.progress = 1.0
        stage.completed_at = now
        stage.heartbeat_at = now
        stage.lease_expires_at = None
        stage.error_message = None

        await self._persist_usage_events(session, stage, usage_events)

        await session.flush()
        await self._activate_dependents(session, stage.id)
        await session.flush()
        run_status = await self._derive_run_status(session, run.id)
        run.status = run_status
        run.completed_at = now if run_status == "completed" else None
        if run_status == "completed":
            run.expires_at = expires_at
            await self._extend_run_artifact_retention(session, run.id, expires_at)

        active_jobs = list(
            await session.scalars(
                select(JobRequest)
                .where(
                    JobRequest.pipeline_run_id == run.id,
                    JobRequest.status.in_({"queued", "running"}),
                )
                .with_for_update()
            )
        )
        for job in active_jobs:
            job.status = run_status
            job.updated_at = now
            if run_status == "completed":
                await enqueue_job_webhook_event(session, job=job, run=run)
        await session.execute(
            update(Worker).where(Worker.id == worker_id).values(last_seen_at=now, status="online", updated_at=now)
        )
        await session.commit()
        completed = await self.get_stage(session, stage_id)
        if completed is None:
            raise RuntimeError("Stage disappeared after completion")
        return completed

    async def fail(
        self,
        session: AsyncSession,
        *,
        worker_id: uuid.UUID,
        stage_id: uuid.UUID,
        lease_token: uuid.UUID,
        error_message: str,
        usage_events: list[UsageCompletion],
    ) -> StageRun:
        now = datetime.now(UTC)
        stage = await self._locked_stage(session, stage_id)
        self._validate_active_lease(stage, worker_id=worker_id, lease_token=lease_token, now=now)
        will_retry = stage.attempt < stage.max_attempts
        stage.status = "queued" if will_retry else "failed"
        stage.error_message = error_message
        stage.lease_token = None
        stage.lease_expires_at = None
        stage.heartbeat_at = now
        stage.progress = None
        if not will_retry:
            stage.completed_at = now

        await self._persist_usage_events(session, stage, usage_events)

        run = await session.get(PipelineRun, stage.pipeline_run_id, with_for_update=True)
        if run is None:
            raise RuntimeError("Pipeline run disappeared while failing a stage")
        run.status = "queued" if will_retry else "failed"
        request_status = "queued" if will_retry else "failed"
        active_jobs = list(
            await session.scalars(
                select(JobRequest)
                .where(
                    JobRequest.pipeline_run_id == run.id,
                    JobRequest.status.in_({"queued", "running"}),
                )
                .with_for_update()
            )
        )
        for job in active_jobs:
            job.status = request_status
            job.updated_at = now
            if request_status == "failed":
                await enqueue_job_webhook_event(session, job=job, run=run)
        await session.commit()
        failed = await self.get_stage(session, stage_id)
        if failed is None:
            raise RuntimeError("Stage disappeared after failure handling")
        return failed

    async def get_stage(self, session: AsyncSession, stage_id: uuid.UUID) -> StageRun | None:
        return await session.scalar(
            select(StageRun)
            .where(StageRun.id == stage_id)
            .options(joinedload(StageRun.pipeline_run).joinedload(PipelineRun.source_blob))
        )

    async def get_stage_inputs(self, session: AsyncSession, stage_id: uuid.UUID) -> list[Artifact]:
        return list(
            await session.scalars(
                select(Artifact)
                .join(
                    StageDependency,
                    StageDependency.depends_on_stage_run_id == Artifact.producer_stage_run_id,
                )
                .where(StageDependency.stage_run_id == stage_id)
                .options(joinedload(Artifact.blob), joinedload(Artifact.producer_stage))
                .order_by(Artifact.artifact_type)
            )
        )

    @staticmethod
    async def _persist_usage_events(
        session: AsyncSession,
        stage: StageRun,
        events: list[UsageCompletion],
    ) -> None:
        if not events:
            return
        provider_ids = {
            provider: provider_id
            for provider, provider_id in await session.execute(
                select(ProviderConfig.provider, ProviderConfig.id).where(
                    ProviderConfig.provider.in_({event.provider for event in events})
                )
            )
        }
        for event in events:
            cost_ticks = event.usage.get("cost_in_usd_ticks")
            if not isinstance(cost_ticks, int):
                cost_ticks = None
            estimated_cost = event.usage.get("estimated_cost_usd")
            try:
                estimated_cost = Decimal(str(estimated_cost)) if estimated_cost is not None else None
            except (InvalidOperation, ValueError):
                estimated_cost = None
            session.add(
                AIUsageEvent(
                    id=event.event_id,
                    stage_run_id=stage.id,
                    provider_config_id=provider_ids.get(event.provider),
                    stage_attempt=stage.attempt,
                    provider=event.provider,
                    model=event.model,
                    operation=event.operation,
                    outcome=event.outcome,
                    usage=event.usage,
                    cost_usd_ticks=cost_ticks,
                    estimated_cost_usd=estimated_cost,
                    latency_ms=event.latency_ms,
                    started_at=event.started_at,
                    completed_at=event.completed_at,
                )
            )

    @staticmethod
    def _validate_artifact_contract(
        pipeline: PipelineDefinition,
        stage: StageRun,
        artifacts: list[ArtifactCompletion],
        schema_version: str,
    ) -> None:
        del schema_version
        definition = pipeline.stage(stage.stage_name)
        reported_types = [artifact.artifact_type for artifact in artifacts]
        if len(reported_types) != len(set(reported_types)):
            raise ArtifactRejected("A stage cannot report the same artifact type more than once")
        if not set(reported_types).issubset(definition.allowed_artifact_types):
            raise ArtifactRejected("Reported artifact does not match the stage output contract")
        if not definition.required_artifact_types.issubset(reported_types):
            raise ArtifactRejected("Stage completion is missing a required artifact")
        expected_versions = {output.artifact_type: output.schema_version for output in definition.outputs}
        if any(artifact.schema_version != expected_versions[artifact.artifact_type] for artifact in artifacts):
            raise ArtifactRejected("Reported artifact schema does not match the stage output contract")

    @staticmethod
    async def _activate_dependents(session: AsyncSession, completed_stage_id: uuid.UUID) -> None:
        dependents = list(
            await session.scalars(
                select(StageRun)
                .join(StageDependency, StageDependency.stage_run_id == StageRun.id)
                .where(
                    StageDependency.depends_on_stage_run_id == completed_stage_id,
                    StageRun.status == "blocked",
                )
                .with_for_update()
            )
        )
        for dependent in dependents:
            incomplete_dependencies = await session.scalar(
                select(func.count())
                .select_from(StageDependency)
                .join(StageRun, StageRun.id == StageDependency.depends_on_stage_run_id)
                .where(
                    StageDependency.stage_run_id == dependent.id,
                    StageRun.status != "completed",
                )
            )
            if incomplete_dependencies == 0:
                dependent.status = "queued"

    @staticmethod
    async def _derive_run_status(session: AsyncSession, run_id: uuid.UUID) -> str:
        statuses = list(await session.scalars(select(StageRun.status).where(StageRun.pipeline_run_id == run_id)))
        if statuses and all(status == "completed" for status in statuses):
            return "completed"
        if any(status == "failed" for status in statuses):
            return "failed"
        if any(status == "running" for status in statuses):
            return "running"
        return "queued"

    @staticmethod
    async def _extend_run_artifact_retention(
        session: AsyncSession,
        run_id: uuid.UUID,
        expires_at: datetime,
    ) -> None:
        artifact_blob_ids = select(Artifact.blob_id).where(Artifact.pipeline_run_id == run_id)
        await session.execute(update(Artifact).where(Artifact.pipeline_run_id == run_id).values(expires_at=expires_at))
        await session.execute(
            update(Blob)
            .where(Blob.id.in_(artifact_blob_ids))
            .values(expires_at=func.greatest(Blob.expires_at, expires_at))
        )

    @staticmethod
    async def _locked_stage(session: AsyncSession, stage_id: uuid.UUID) -> StageRun:
        stage = await session.scalar(select(StageRun).where(StageRun.id == stage_id).with_for_update())
        if stage is None:
            raise LeaseLost(f"Stage {stage_id} was not found")
        return stage

    @staticmethod
    def _validate_lease_identity(stage: StageRun, *, worker_id: uuid.UUID, lease_token: uuid.UUID) -> None:
        if stage.lease_owner_id != worker_id or stage.lease_token != lease_token:
            raise LeaseLost("Stage lease belongs to another worker")

    @classmethod
    def _validate_active_lease(
        cls,
        stage: StageRun,
        *,
        worker_id: uuid.UUID,
        lease_token: uuid.UUID,
        now: datetime,
    ) -> None:
        cls._validate_lease_identity(stage, worker_id=worker_id, lease_token=lease_token)
        if stage.status != "running" or stage.lease_expires_at is None or stage.lease_expires_at <= now:
            raise LeaseLost("Stage lease is no longer active")
