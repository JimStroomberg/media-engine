from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import select, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import joinedload

from ..config import Settings
from ..domain.content import pipeline_run_key
from ..domain.pipelines import PipelineDefinition, required_capabilities_for_stage
from ..persistence.models import Artifact, Asset, Blob, JobRequest, PipelineRun, StageDependency, StageRun
from ..storage.s3 import S3Store
from .webhooks import enqueue_job_webhook_event


class AssetNotFound(RuntimeError):
    pass


class AssetUnavailable(RuntimeError):
    pass


class IdempotencyConflict(RuntimeError):
    pass


@dataclass(frozen=True)
class JobSubmissionResult:
    job: JobRequest
    idempotent_replay: bool


@dataclass(frozen=True)
class JobManifestData:
    job: JobRequest
    stages: tuple[StageRun, ...]
    dependencies: tuple[StageDependency, ...]
    artifacts: tuple[Artifact, ...]


class JobService:
    def __init__(self, settings: Settings, store: S3Store) -> None:
        self.settings = settings
        self.store = store

    async def submit(
        self,
        session: AsyncSession,
        *,
        client_id: uuid.UUID,
        asset_id: uuid.UUID,
        pipeline: PipelineDefinition,
        options: dict[str, Any],
        client_job_id: str | None,
        callback_url: str | None,
        webhook_endpoint_id: uuid.UUID | None,
        idempotency_key: str | None,
    ) -> JobSubmissionResult:
        now = datetime.now(UTC)
        asset = await session.scalar(
            select(Asset).where(Asset.id == asset_id, Asset.client_id == client_id)
        )
        if asset is None:
            raise AssetNotFound(f"Asset {asset_id} was not found")

        blob = await session.scalar(select(Blob).where(Blob.id == asset.blob_id).with_for_update())
        if (
            asset.expires_at <= now
            or blob is None
            or blob.state != "available"
            or blob.expires_at <= now
            or not await self.store.exists(blob.object_key)
        ):
            raise AssetUnavailable(f"Asset {asset_id} is no longer available")

        run_key = pipeline_run_key(
            source_sha256=blob.sha256,
            pipeline_name=pipeline.name,
            pipeline_version=pipeline.version,
            options=options,
            processor_versions=pipeline.processor_versions,
            schema_version=pipeline.schema_version,
        )

        if idempotency_key is not None:
            replay = await self._get_by_idempotency_key(session, client_id, idempotency_key)
            if replay is not None:
                self._validate_replay(
                    replay,
                    asset_id=asset_id,
                    run_key=run_key,
                    webhook_endpoint_id=webhook_endpoint_id,
                )
                return JobSubmissionResult(job=replay, idempotent_replay=True)

        run_expires_at = now + timedelta(hours=self.settings.pipeline_retention_hours)
        candidate_run_id = uuid.uuid4()
        run_insert = (
            insert(PipelineRun)
            .values(
                id=candidate_run_id,
                source_blob_id=blob.id,
                run_key=run_key,
                pipeline_name=pipeline.name,
                pipeline_version=pipeline.version,
                schema_version=pipeline.schema_version,
                options=options,
                processor_versions=dict(pipeline.processor_versions),
                status="queued",
                expires_at=run_expires_at,
            )
            .on_conflict_do_nothing(index_elements=[PipelineRun.run_key])
            .returning(PipelineRun.id)
        )
        inserted_run_id = (await session.execute(run_insert)).scalar_one_or_none()
        run_reused = inserted_run_id is None
        run = await session.scalar(select(PipelineRun).where(PipelineRun.run_key == run_key).with_for_update())
        if run is None:
            raise RuntimeError("Pipeline run disappeared after atomic creation")

        cache_hit = await self._is_cache_hit(
            session,
            run,
            pipeline,
            now,
            extend_to=run_expires_at,
        )
        if cache_hit:
            request_status = "completed"
        elif run.status in {"queued", "running"} and run.expires_at > now:
            request_status = run.status
            if run.status == "queued":
                await self._ensure_stages(session, run, pipeline, reset=False)
        else:
            run.status = "queued"
            run.completed_at = None
            run.expires_at = run_expires_at
            request_status = "queued"
            await self._ensure_stages(session, run, pipeline, reset=True)

        candidate_job_id = uuid.uuid4()
        job_values = {
            "id": candidate_job_id,
            "client_id": client_id,
            "pipeline_run_id": run.id,
            "asset_id": asset_id,
            "client_job_id": client_job_id,
            "idempotency_key": idempotency_key,
            "callback_url": callback_url,
            "webhook_endpoint_id": webhook_endpoint_id,
            "status": request_status,
            "cache_hit": cache_hit,
            "run_reused": run_reused,
        }
        job_insert = insert(JobRequest).values(**job_values)
        if idempotency_key is not None:
            job_insert = job_insert.on_conflict_do_nothing(
                constraint="uq_job_request_client_idempotency"
            )
        job_insert = job_insert.returning(JobRequest.id)
        inserted_job_id = (await session.execute(job_insert)).scalar_one_or_none()

        if inserted_job_id is None:
            replay = await self._get_by_idempotency_key(session, client_id, idempotency_key)
            if replay is None:
                raise RuntimeError("Idempotent job request disappeared after conflict")
            self._validate_replay(
                replay,
                asset_id=asset_id,
                run_key=run_key,
                webhook_endpoint_id=webhook_endpoint_id,
            )
            await session.rollback()
            replay = await self._get_by_idempotency_key(session, client_id, idempotency_key)
            if replay is None:
                raise RuntimeError("Idempotent job request disappeared after rollback")
            return JobSubmissionResult(job=replay, idempotent_replay=True)

        job = await session.scalar(select(JobRequest).where(JobRequest.id == inserted_job_id))
        if job is None:
            raise RuntimeError("Job request disappeared before commit")
        if job.status == "completed":
            await enqueue_job_webhook_event(session, job=job, run=run)

        await session.commit()
        job = await self.get(session, inserted_job_id, client_id=client_id)
        if job is None:
            raise RuntimeError("Job request disappeared after commit")
        return JobSubmissionResult(job=job, idempotent_replay=False)

    async def get(
        self,
        session: AsyncSession,
        job_id: uuid.UUID,
        *,
        client_id: uuid.UUID,
    ) -> JobRequest | None:
        return await session.scalar(
            select(JobRequest)
            .where(JobRequest.id == job_id, JobRequest.client_id == client_id)
            .options(joinedload(JobRequest.pipeline_run), joinedload(JobRequest.asset))
        )

    async def list_artifacts(
        self,
        session: AsyncSession,
        job_id: uuid.UUID,
        *,
        client_id: uuid.UUID,
    ) -> list[Artifact] | None:
        job = await self.get(session, job_id, client_id=client_id)
        if job is None:
            return None
        return list(
            await session.scalars(
                select(Artifact)
                .where(Artifact.pipeline_run_id == job.pipeline_run_id)
                .options(joinedload(Artifact.blob))
                .order_by(Artifact.artifact_type)
            )
        )

    async def manifest(
        self,
        session: AsyncSession,
        job_id: uuid.UUID,
        *,
        client_id: uuid.UUID,
    ) -> JobManifestData | None:
        job = await session.scalar(
            select(JobRequest)
            .where(JobRequest.id == job_id, JobRequest.client_id == client_id)
            .options(
                joinedload(JobRequest.asset).joinedload(Asset.blob),
                joinedload(JobRequest.pipeline_run).joinedload(PipelineRun.source_blob),
                joinedload(JobRequest.pipeline_run).selectinload(PipelineRun.stages),
                joinedload(JobRequest.pipeline_run)
                .selectinload(PipelineRun.artifacts)
                .joinedload(Artifact.blob),
            )
        )
        if job is None:
            return None

        run = job.pipeline_run
        dependencies = tuple(
            await session.scalars(
                select(StageDependency)
                .join(StageRun, StageRun.id == StageDependency.stage_run_id)
                .where(StageRun.pipeline_run_id == run.id)
                .order_by(StageDependency.stage_run_id, StageDependency.depends_on_stage_run_id)
            )
        )
        return JobManifestData(
            job=job,
            stages=tuple(run.stages),
            dependencies=dependencies,
            artifacts=tuple(sorted(run.artifacts, key=lambda artifact: artifact.artifact_type)),
        )

    async def _get_by_idempotency_key(
        self,
        session: AsyncSession,
        client_id: uuid.UUID,
        idempotency_key: str | None,
    ) -> JobRequest | None:
        if idempotency_key is None:
            return None
        return await session.scalar(
            select(JobRequest)
            .where(
                JobRequest.client_id == client_id,
                JobRequest.idempotency_key == idempotency_key,
            )
            .options(joinedload(JobRequest.pipeline_run), joinedload(JobRequest.asset))
        )

    async def _is_cache_hit(
        self,
        session: AsyncSession,
        run: PipelineRun,
        pipeline: PipelineDefinition,
        now: datetime,
        *,
        extend_to: datetime,
    ) -> bool:
        if run.status != "completed" or run.expires_at <= now:
            return False

        rows = list(
            await session.execute(
                select(Artifact, Blob)
                .join(Blob, Blob.id == Artifact.blob_id)
                .where(
                    Artifact.pipeline_run_id == run.id,
                    Artifact.artifact_type.in_(pipeline.required_artifacts),
                )
                .with_for_update(of=(Artifact, Blob))
            )
        )
        available_types: set[str] = set()
        for artifact, artifact_blob in rows:
            if (
                artifact.expires_at > now
                and artifact_blob.state == "available"
                and artifact_blob.expires_at > now
                and await self.store.exists(artifact_blob.object_key)
            ):
                available_types.add(artifact.artifact_type)
        if not set(pipeline.required_artifacts).issubset(available_types):
            return False

        # A new job request is an explicit live reference, so it extends the
        # retained cached outputs. Passive reads do not extend retention.
        run.expires_at = max(run.expires_at, extend_to)
        for artifact, artifact_blob in rows:
            artifact.expires_at = max(artifact.expires_at, extend_to)
            artifact_blob.expires_at = max(artifact_blob.expires_at, extend_to)
        return True

    async def _ensure_stages(
        self,
        session: AsyncSession,
        run: PipelineRun,
        pipeline: PipelineDefinition,
        *,
        reset: bool,
    ) -> None:
        existing_stages = {
            stage.stage_name: stage
            for stage in await session.scalars(select(StageRun).where(StageRun.pipeline_run_id == run.id))
        }
        for stage_definition in pipeline.stages:
            if stage_definition.name in existing_stages:
                continue
            required_capabilities = required_capabilities_for_stage(stage_definition, run.options)
            if pipeline.name == "transcode" and self.settings.transcode_required_backend:
                required_capabilities["backends"] = [self.settings.transcode_required_backend]
            statement = (
                insert(StageRun)
                .values(
                    id=uuid.uuid4(),
                    pipeline_run_id=run.id,
                    stage_name=stage_definition.name,
                    status="blocked" if stage_definition.depends_on else "queued",
                    required_capabilities=required_capabilities,
                )
                .on_conflict_do_nothing(constraint="uq_stage_run_pipeline_stage")
            )
            await session.execute(statement)

        stages_by_name = {
            stage.stage_name: stage
            for stage in await session.scalars(select(StageRun).where(StageRun.pipeline_run_id == run.id))
        }
        for stage_definition in pipeline.stages:
            stage_run = stages_by_name[stage_definition.name]
            for dependency_name in stage_definition.depends_on:
                dependency = stages_by_name[dependency_name]
                await session.execute(
                    insert(StageDependency)
                    .values(
                        stage_run_id=stage_run.id,
                        depends_on_stage_run_id=dependency.id,
                    )
                    .on_conflict_do_nothing()
                )

        if reset:
            for stage_definition in pipeline.stages:
                await session.execute(
                    update(StageRun)
                    .where(StageRun.id == stages_by_name[stage_definition.name].id)
                    .values(
                        status="blocked" if stage_definition.depends_on else "queued",
                        attempt=0,
                        lease_owner_id=None,
                        lease_token=None,
                        lease_expires_at=None,
                        heartbeat_at=None,
                        progress=None,
                        error_message=None,
                        started_at=None,
                        completed_at=None,
                    )
                )

    @staticmethod
    def _validate_replay(
        job: JobRequest,
        *,
        asset_id: uuid.UUID,
        run_key: str,
        webhook_endpoint_id: uuid.UUID | None,
    ) -> None:
        if (
            job.asset_id != asset_id
            or job.pipeline_run.run_key != run_key
            or job.webhook_endpoint_id != webhook_endpoint_id
        ):
            raise IdempotencyConflict("Idempotency-Key was already used for a different request")
