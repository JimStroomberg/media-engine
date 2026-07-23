from __future__ import annotations

import argparse
import asyncio
import logging
from datetime import UTC, datetime, timedelta

from sqlalchemy import select, update

from .config import get_settings
from .persistence.database import close_database, configure_database, get_session_factory
from .persistence.models import Blob, JobRequest, PipelineRun, StageRun, Worker
from .services.webhooks import enqueue_job_webhook_event
from .storage.s3 import S3Store

logger = logging.getLogger(__name__)


async def expire_blobs_once(store: S3Store) -> int:
    settings = get_settings()
    factory = get_session_factory()

    expired_count = 0
    for _ in range(settings.retention_batch_size):
        deletion_failed = False
        async with factory.begin() as session:
            blob = await session.scalar(
                select(Blob)
                .where(Blob.state == "available", Blob.expires_at <= datetime.now(UTC))
                .order_by(Blob.expires_at)
                .limit(1)
                .with_for_update(skip_locked=True)
            )
            if blob is None:
                break

            # Keep the row locked until the object deletion and state update
            # commit together. Ingest takes the same lock for an existing hash,
            # preventing a restored object from being deleted by this sweep.
            blob.state = "deleting"
            try:
                await store.delete(blob.object_key)
            except Exception:  # noqa: BLE001
                logger.exception("Blob deletion failed", extra={"blob_id": str(blob.id), "key": blob.object_key})
                blob.state = "available"
                deletion_failed = True
            else:
                blob.state = "expired"
                blob.expired_at = datetime.now(UTC)

        if deletion_failed:
            # Avoid repeatedly selecting the same failing blob in one sweep.
            # The next scheduled sweep will retry the idempotent deletion.
            break

        expired_count += 1
    return expired_count


async def recover_expired_leases_once() -> int:
    """Requeue abandoned stages, or fail them after their final attempt."""

    settings = get_settings()
    factory = get_session_factory()
    now = datetime.now(UTC)
    recovered_count = 0
    async with factory.begin() as session:
        stages = list(
            await session.scalars(
                select(StageRun)
                .where(
                    StageRun.status == "running",
                    StageRun.lease_expires_at.is_not(None),
                    StageRun.lease_expires_at <= now,
                )
                .order_by(StageRun.lease_expires_at)
                .limit(settings.retention_batch_size)
                .with_for_update(skip_locked=True)
            )
        )
        for stage in stages:
            will_retry = stage.attempt < stage.max_attempts
            stage.status = "queued" if will_retry else "failed"
            stage.error_message = "Worker lease expired"
            stage.lease_owner_id = None
            stage.lease_token = None
            stage.lease_expires_at = None
            stage.progress = None
            if not will_retry:
                stage.completed_at = now

            run = await session.get(PipelineRun, stage.pipeline_run_id, with_for_update=True)
            if run is not None:
                run.status = "queued" if will_retry else "failed"
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
                    job.status = run.status
                    job.updated_at = now
                    if run.status == "failed":
                        await enqueue_job_webhook_event(session, job=job, run=run)
            recovered_count += 1

        stale_cutoff = now - timedelta(seconds=settings.worker_lease_seconds * 2)
        await session.execute(
            update(Worker).where(Worker.last_seen_at < stale_cutoff).values(status="offline", updated_at=now)
        )
    return recovered_count


async def run_scheduler(*, once: bool) -> None:
    settings = get_settings()
    if not settings.platform_v2_enabled or not settings.database_url:
        raise RuntimeError("Scheduler requires MEDIA_ENGINE_DATABASE_URL and MEDIA_ENGINE_S3_BUCKET")
    configure_database(settings.database_url)
    store = S3Store(settings)
    await store.check_bucket()
    try:
        while True:
            recovered = await recover_expired_leases_once()
            expired = await expire_blobs_once(store)
            logger.info(
                "Maintenance sweep completed",
                extra={"expired_blobs": expired, "recovered_leases": recovered},
            )
            if once:
                return
            await asyncio.sleep(settings.retention_interval_seconds)
    finally:
        await close_database()


def main() -> None:
    parser = argparse.ArgumentParser(description="Expire Media Engine S3 blobs")
    parser.add_argument("--once", action="store_true", help="Run one retention sweep and exit")
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO)
    asyncio.run(run_scheduler(once=args.once))


if __name__ == "__main__":
    main()
