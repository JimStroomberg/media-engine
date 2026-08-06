from __future__ import annotations

import secrets
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from ..persistence.models import StageRun, Worker
from ..security import GeneratedWorkerToken, generate_worker_token, hash_api_key

WORKER_DESIRED_STATES = frozenset({"active", "draining", "revoked"})


class WorkerConflict(RuntimeError):
    pass


class WorkerStateConflict(RuntimeError):
    pass


class WorkerRemovalConflict(RuntimeError):
    pass


@dataclass(frozen=True)
class WorkerPrincipal:
    worker_id: uuid.UUID


def _worker_token_parts(token: str) -> tuple[str, str] | None:
    parts = token.split("_", 2)
    if len(parts) != 3 or parts[0] != "mew" or not parts[1] or not parts[2]:
        return None
    return parts[1], hash_api_key(token)


class WorkerAccessService:
    async def create(
        self,
        session: AsyncSession,
        *,
        display_name: str,
        profile: str,
        expires_at: datetime | None,
    ) -> tuple[Worker, GeneratedWorkerToken]:
        generated = generate_worker_token()
        worker = Worker(
            id=uuid.uuid4(),
            worker_key=f"worker-{generated.prefix}",
            display_name=display_name,
            profile=profile,
            capabilities={},
            runtime={},
            status="offline",
            desired_state="active",
            credential_prefix=generated.prefix,
            credential_hash=generated.token_hash,
            credential_created_at=datetime.now(UTC),
            credential_expires_at=expires_at,
            credential_revoked_at=None,
            registered_at=None,
            last_seen_at=None,
        )
        session.add(worker)
        await session.commit()
        await session.refresh(worker)
        return worker, generated

    async def authenticate(self, session: AsyncSession, token: str) -> WorkerPrincipal | None:
        token_parts = _worker_token_parts(token)
        if token_parts is None:
            return None
        prefix, supplied_hash = token_parts
        worker = await session.scalar(
            select(Worker).where(Worker.credential_prefix == prefix, Worker.removed_at.is_(None))
        )
        now = datetime.now(UTC)
        if (
            worker is None
            or worker.credential_hash is None
            or worker.removed_at is not None
            or worker.credential_revoked_at is not None
            or worker.desired_state == "revoked"
            or (worker.credential_expires_at is not None and worker.credential_expires_at <= now)
            or not secrets.compare_digest(worker.credential_hash, supplied_hash)
        ):
            return None
        worker.credential_last_used_at = now
        await session.commit()
        return WorkerPrincipal(worker_id=worker.id)

    async def rotate(
        self,
        session: AsyncSession,
        worker: Worker,
        *,
        expires_at: datetime | None,
    ) -> GeneratedWorkerToken:
        generated = generate_worker_token()
        worker.credential_prefix = generated.prefix
        worker.credential_hash = generated.token_hash
        worker.credential_created_at = datetime.now(UTC)
        worker.credential_last_used_at = None
        worker.credential_revoked_at = None
        worker.credential_expires_at = expires_at
        worker.desired_state = "active"
        await session.commit()
        await session.refresh(worker)
        return generated

    async def update(
        self,
        session: AsyncSession,
        worker: Worker,
        *,
        display_name: str | None,
        profile: str | None,
    ) -> Worker:
        if display_name is not None:
            worker.display_name = display_name
        if profile is not None:
            worker.profile = profile
        await session.commit()
        await session.refresh(worker)
        return worker

    async def set_desired_state(self, session: AsyncSession, worker: Worker, desired_state: str) -> Worker:
        if worker.removed_at is not None:
            raise WorkerStateConflict("Removed workers cannot change lifecycle state")
        if desired_state not in WORKER_DESIRED_STATES:
            raise ValueError(f"Unsupported worker state {desired_state!r}")
        if worker.desired_state == "revoked" and desired_state != "revoked":
            raise WorkerStateConflict("Rotate the worker token to reactivate a revoked worker")
        worker.desired_state = desired_state
        if desired_state == "revoked":
            worker.credential_revoked_at = worker.credential_revoked_at or datetime.now(UTC)
            worker.status = "offline"
        await session.commit()
        await session.refresh(worker)
        return worker

    async def remove(self, session: AsyncSession, worker: Worker) -> None:
        """Hide a revoked worker while retaining its historical stage attribution."""

        if worker.removed_at is not None:
            raise WorkerRemovalConflict("Worker has already been removed")
        if worker.desired_state != "revoked":
            raise WorkerRemovalConflict("Revoke the worker before removing it")
        active_leases = await session.scalar(
            select(func.count())
            .select_from(StageRun)
            .where(StageRun.lease_owner_id == worker.id, StageRun.status == "running")
        )
        if active_leases:
            raise WorkerRemovalConflict("Wait for the worker's active lease to expire before removing it")

        now = datetime.now(UTC)
        worker.removed_at = now
        worker.status = "offline"
        worker.credential_prefix = None
        worker.credential_hash = None
        worker.credential_expires_at = None
        worker.credential_last_used_at = None
        worker.credential_revoked_at = worker.credential_revoked_at or now
        await session.commit()

    async def import_initial(
        self,
        session: AsyncSession,
        *,
        worker_key: str,
        display_name: str,
        profile: str,
        token: str,
    ) -> Worker:
        """Create or refresh the explicitly configured bundled worker identity."""

        token_parts = _worker_token_parts(token)
        if token_parts is None:
            raise ValueError("MEDIA_ENGINE_INITIAL_WORKER_TOKEN must use the mew_<prefix>_<secret> format")
        prefix, token_hash = token_parts
        conflicting = await session.scalar(
            select(Worker.id).where(Worker.credential_prefix == prefix, Worker.worker_key != worker_key)
        )
        if conflicting is not None:
            raise WorkerConflict("Initial worker token prefix is already assigned to another worker")
        worker = await session.scalar(select(Worker).where(Worker.worker_key == worker_key))
        now = datetime.now(UTC)
        if worker is None:
            worker = Worker(
                id=uuid.uuid4(),
                worker_key=worker_key,
                display_name=display_name,
                profile=profile,
                capabilities={},
                runtime={},
                status="offline",
                desired_state="active",
                credential_prefix=prefix,
                credential_hash=token_hash,
                credential_created_at=now,
                registered_at=None,
                last_seen_at=None,
            )
            session.add(worker)
        else:
            if worker.removed_at is not None:
                raise WorkerConflict(
                    "The configured initial worker was removed. Remove its bundled-worker configuration or choose a "
                    "new MEDIA_ENGINE_INITIAL_WORKER_KEY before retrying startup."
                )
            worker.display_name = display_name
            worker.profile = profile
            if worker.credential_hash is None:
                # Enrol a legacy row once during the managed-worker migration.
                worker.credential_prefix = prefix
                worker.credential_hash = token_hash
                worker.credential_created_at = now
                worker.credential_last_used_at = None
                worker.credential_revoked_at = None
                worker.desired_state = "active"
            elif not secrets.compare_digest(worker.credential_hash, token_hash):
                raise WorkerConflict(
                    "Configured initial worker token does not match its managed identity. "
                    "Update MEDIA_ENGINE_LOCAL_WORKER_TOKEN with the latest rotated token and retry startup."
                )
            # Matching credentials are already managed in PostgreSQL. Preserve drain,
            # revoke, expiry, and token-use metadata across control-plane restarts.
        await session.commit()
        await session.refresh(worker)
        return worker
