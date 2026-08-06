from __future__ import annotations

import uuid
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import (
    JSON,
    BigInteger,
    Boolean,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


class Base(DeclarativeBase):
    pass


class ClientProject(Base):
    __tablename__ = "clients"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    name: Mapped[str] = mapped_column(String(255), nullable=False, unique=True)
    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )

    api_keys: Mapped[list[ApiKey]] = relationship(back_populates="client", cascade="all, delete-orphan")
    assets: Mapped[list[Asset]] = relationship(back_populates="client")
    job_requests: Mapped[list[JobRequest]] = relationship(back_populates="client")
    webhook_endpoints: Mapped[list[WebhookEndpoint]] = relationship(
        back_populates="client", cascade="all, delete-orphan"
    )
    webhook_events: Mapped[list[WebhookEvent]] = relationship(back_populates="client")


class ApiKey(Base):
    __tablename__ = "api_keys"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    client_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("clients.id", ondelete="CASCADE"), nullable=False, index=True
    )
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    key_prefix: Mapped[str] = mapped_column(String(32), nullable=False, unique=True)
    key_hash: Mapped[str] = mapped_column(String(64), nullable=False, unique=True)
    scopes: Mapped[list[str]] = mapped_column(JSON, nullable=False, default=list)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_used_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    client: Mapped[ClientProject] = relationship(back_populates="api_keys")

    __table_args__ = (UniqueConstraint("client_id", "name", name="uq_api_key_client_name"),)


class ProviderConfig(Base):
    __tablename__ = "provider_configs"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    name: Mapped[str] = mapped_column(String(255), nullable=False, unique=True)
    provider: Mapped[str] = mapped_column(String(32), nullable=False, unique=True)
    encrypted_api_key: Mapped[str] = mapped_column(Text, nullable=False)
    credential_hint: Mapped[str] = mapped_column(String(64), nullable=False)
    base_url: Mapped[str] = mapped_column(Text, nullable=False)
    models: Mapped[dict[str, str]] = mapped_column(JSON, nullable=False, default=dict)
    runtime_options: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)
    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    is_default: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )
    last_verified_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_error: Mapped[str | None] = mapped_column(Text)

    usage_events: Mapped[list[AIUsageEvent]] = relationship(back_populates="provider_config")

    __table_args__ = (Index("uq_provider_configs_default", "is_default", unique=True, postgresql_where=is_default),)


class WebhookEndpoint(Base):
    __tablename__ = "webhook_endpoints"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    client_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("clients.id", ondelete="CASCADE"), nullable=False, index=True
    )
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    url: Mapped[str] = mapped_column(Text, nullable=False)
    encrypted_signing_secret: Mapped[str] = mapped_column(Text, nullable=False)
    signing_secret_hint: Mapped[str] = mapped_column(String(64), nullable=False)
    events: Mapped[list[str]] = mapped_column(JSON, nullable=False, default=list)
    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    is_default: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )

    client: Mapped[ClientProject] = relationship(back_populates="webhook_endpoints")
    job_requests: Mapped[list[JobRequest]] = relationship(back_populates="webhook_endpoint")
    webhook_events: Mapped[list[WebhookEvent]] = relationship(back_populates="endpoint")

    __table_args__ = (
        UniqueConstraint("client_id", "name", name="uq_webhook_endpoint_client_name"),
        Index(
            "uq_webhook_endpoints_client_default",
            "client_id",
            unique=True,
            postgresql_where=is_default,
        ),
    )


class Blob(Base):
    __tablename__ = "blobs"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    sha256: Mapped[str] = mapped_column(String(64), nullable=False, unique=True)
    size_bytes: Mapped[int] = mapped_column(BigInteger, nullable=False)
    media_type: Mapped[str | None] = mapped_column(String(255))
    bucket: Mapped[str] = mapped_column(String(255), nullable=False)
    object_key: Mapped[str] = mapped_column(Text, nullable=False, unique=True)
    etag: Mapped[str | None] = mapped_column(String(255))
    state: Mapped[str] = mapped_column(String(32), nullable=False, default="available")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    expired_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    assets: Mapped[list[Asset]] = relationship(back_populates="blob")
    artifacts: Mapped[list[Artifact]] = relationship(back_populates="blob")
    pipeline_runs: Mapped[list[PipelineRun]] = relationship(back_populates="source_blob")

    __table_args__ = (Index("ix_blobs_state_expires_at", "state", "expires_at"),)


class Asset(Base):
    __tablename__ = "assets"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    client_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("clients.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    blob_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("blobs.id", ondelete="RESTRICT"), nullable=False, index=True)
    source_filename: Mapped[str] = mapped_column(Text, nullable=False)
    source_kind: Mapped[str] = mapped_column(String(64), nullable=False, default="upload")
    source_uri: Mapped[str | None] = mapped_column(Text)
    source_metadata: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)

    blob: Mapped[Blob] = relationship(back_populates="assets", lazy="joined")
    client: Mapped[ClientProject] = relationship(back_populates="assets")
    job_requests: Mapped[list[JobRequest]] = relationship(back_populates="asset")


class PipelineRun(Base):
    __tablename__ = "pipeline_runs"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    source_blob_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("blobs.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    run_key: Mapped[str] = mapped_column(String(64), nullable=False, unique=True)
    pipeline_name: Mapped[str] = mapped_column(String(128), nullable=False)
    pipeline_version: Mapped[str] = mapped_column(String(64), nullable=False)
    schema_version: Mapped[str] = mapped_column(String(64), nullable=False, default="1")
    options: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)
    processor_versions: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="queued")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)

    source_blob: Mapped[Blob] = relationship(back_populates="pipeline_runs")
    artifacts: Mapped[list[Artifact]] = relationship(back_populates="pipeline_run")
    requests: Mapped[list[JobRequest]] = relationship(back_populates="pipeline_run")
    stages: Mapped[list[StageRun]] = relationship(back_populates="pipeline_run")

    __table_args__ = (Index("ix_pipeline_runs_status_created_at", "status", "created_at"),)


class Artifact(Base):
    __tablename__ = "artifacts"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    pipeline_run_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("pipeline_runs.id", ondelete="CASCADE"), nullable=False, index=True
    )
    producer_stage_run_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("stage_runs.id", ondelete="CASCADE"), nullable=False, index=True
    )
    blob_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("blobs.id", ondelete="RESTRICT"), nullable=False, index=True)
    artifact_type: Mapped[str] = mapped_column(String(128), nullable=False)
    format: Mapped[str] = mapped_column(String(64), nullable=False)
    schema_version: Mapped[str] = mapped_column(String(64), nullable=False, default="1")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)

    pipeline_run: Mapped[PipelineRun] = relationship(back_populates="artifacts")
    producer_stage: Mapped[StageRun] = relationship(back_populates="artifacts")
    blob: Mapped[Blob] = relationship(back_populates="artifacts")

    __table_args__ = (UniqueConstraint("pipeline_run_id", "artifact_type", name="uq_artifact_run_type"),)


class JobRequest(Base):
    __tablename__ = "job_requests"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    client_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("clients.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    pipeline_run_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("pipeline_runs.id", ondelete="CASCADE"), nullable=False, index=True
    )
    asset_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("assets.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    client_job_id: Mapped[str | None] = mapped_column(String(255), index=True)
    idempotency_key: Mapped[str | None] = mapped_column(String(255))
    callback_url: Mapped[str | None] = mapped_column(Text)
    webhook_endpoint_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("webhook_endpoints.id", ondelete="SET NULL"), index=True
    )
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="queued")
    cache_hit: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    run_reused: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )

    pipeline_run: Mapped[PipelineRun] = relationship(back_populates="requests")
    asset: Mapped[Asset] = relationship(back_populates="job_requests")
    client: Mapped[ClientProject] = relationship(back_populates="job_requests")
    webhook_endpoint: Mapped[WebhookEndpoint | None] = relationship(back_populates="job_requests")
    webhook_events: Mapped[list[WebhookEvent]] = relationship(back_populates="job_request")

    __table_args__ = (
        UniqueConstraint("client_id", "idempotency_key", name="uq_job_request_client_idempotency"),
    )


class Worker(Base):
    __tablename__ = "workers"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    worker_key: Mapped[str] = mapped_column(String(255), nullable=False, unique=True)
    display_name: Mapped[str] = mapped_column(String(255), nullable=False)
    profile: Mapped[str] = mapped_column(String(64), nullable=False, default="cpu")
    capabilities: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)
    runtime: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="online")
    desired_state: Mapped[str] = mapped_column(String(32), nullable=False, default="active")
    credential_prefix: Mapped[str | None] = mapped_column(String(32), unique=True)
    credential_hash: Mapped[str | None] = mapped_column(String(64), unique=True)
    credential_created_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    credential_expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    credential_last_used_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    credential_revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    removed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    registered_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_seen_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )

    stages: Mapped[list[StageRun]] = relationship(back_populates="lease_owner")


class StageRun(Base):
    __tablename__ = "stage_runs"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    pipeline_run_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("pipeline_runs.id", ondelete="CASCADE"), nullable=False, index=True
    )
    stage_name: Mapped[str] = mapped_column(String(128), nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="queued")
    required_capabilities: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)
    priority: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    attempt: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    max_attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=3)
    lease_owner_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("workers.id", ondelete="SET NULL"), index=True)
    lease_token: Mapped[uuid.UUID | None] = mapped_column(unique=True)
    lease_expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    heartbeat_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    progress: Mapped[float | None] = mapped_column(Float)
    error_message: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    pipeline_run: Mapped[PipelineRun] = relationship(back_populates="stages")
    lease_owner: Mapped[Worker | None] = relationship(back_populates="stages")
    artifacts: Mapped[list[Artifact]] = relationship(back_populates="producer_stage")
    usage_events: Mapped[list[AIUsageEvent]] = relationship(back_populates="stage_run", cascade="all, delete-orphan")

    __table_args__ = (
        UniqueConstraint("pipeline_run_id", "stage_name", name="uq_stage_run_pipeline_stage"),
        Index("ix_stage_runs_status_priority_created_at", "status", "priority", "created_at"),
    )


class StageDependency(Base):
    __tablename__ = "stage_dependencies"

    stage_run_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("stage_runs.id", ondelete="CASCADE"), primary_key=True
    )
    depends_on_stage_run_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("stage_runs.id", ondelete="CASCADE"), primary_key=True, index=True
    )


class AIUsageEvent(Base):
    __tablename__ = "ai_usage_events"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    stage_run_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("stage_runs.id", ondelete="CASCADE"), nullable=False, index=True
    )
    provider_config_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("provider_configs.id", ondelete="SET NULL"), index=True
    )
    stage_attempt: Mapped[int] = mapped_column(Integer, nullable=False)
    provider: Mapped[str] = mapped_column(String(32), nullable=False)
    model: Mapped[str] = mapped_column(String(128), nullable=False)
    operation: Mapped[str] = mapped_column(String(64), nullable=False)
    outcome: Mapped[str] = mapped_column(String(32), nullable=False)
    usage: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)
    cost_usd_ticks: Mapped[int | None] = mapped_column(BigInteger)
    estimated_cost_usd: Mapped[Decimal | None] = mapped_column(Numeric(20, 10))
    latency_ms: Mapped[int] = mapped_column(Integer, nullable=False)
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    completed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)

    stage_run: Mapped[StageRun] = relationship(back_populates="usage_events")
    provider_config: Mapped[ProviderConfig | None] = relationship(back_populates="usage_events")

    __table_args__ = (
        Index("ix_ai_usage_events_provider_created_at", "provider", "created_at"),
        Index("ix_ai_usage_events_stage_attempt", "stage_run_id", "stage_attempt"),
    )


class WebhookEvent(Base):
    __tablename__ = "webhook_events"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    client_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("clients.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    endpoint_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("webhook_endpoints.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    job_request_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("job_requests.id", ondelete="CASCADE"), index=True
    )
    event_type: Mapped[str] = mapped_column(String(64), nullable=False)
    payload: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="pending")
    attempt_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    next_attempt_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=lambda: datetime.now(UTC)
    )
    lease_token: Mapped[uuid.UUID | None] = mapped_column(unique=True)
    lease_expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    delivered_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    abandoned_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_error: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )

    client: Mapped[ClientProject] = relationship(back_populates="webhook_events")
    endpoint: Mapped[WebhookEndpoint] = relationship(back_populates="webhook_events")
    job_request: Mapped[JobRequest | None] = relationship(back_populates="webhook_events")
    attempts: Mapped[list[WebhookDeliveryAttempt]] = relationship(
        back_populates="webhook_event", cascade="all, delete-orphan"
    )

    __table_args__ = (
        UniqueConstraint("job_request_id", "event_type", name="uq_webhook_event_job_type"),
        Index("ix_webhook_events_dispatch", "status", "next_attempt_at"),
    )


class WebhookDeliveryAttempt(Base):
    __tablename__ = "webhook_delivery_attempts"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    webhook_event_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("webhook_events.id", ondelete="CASCADE"), nullable=False, index=True
    )
    attempt_number: Mapped[int] = mapped_column(Integer, nullable=False)
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    completed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    duration_ms: Mapped[int] = mapped_column(Integer, nullable=False)
    response_status: Mapped[int | None] = mapped_column(Integer)
    response_body_preview: Mapped[str | None] = mapped_column(Text)
    error_message: Mapped[str | None] = mapped_column(Text)
    outcome: Mapped[str] = mapped_column(String(32), nullable=False)

    webhook_event: Mapped[WebhookEvent] = relationship(back_populates="attempts")

    __table_args__ = (
        UniqueConstraint("webhook_event_id", "attempt_number", name="uq_webhook_delivery_event_attempt"),
    )
