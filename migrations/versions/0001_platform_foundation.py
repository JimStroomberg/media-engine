"""Create S3 asset and pipeline foundation.

Revision ID: 0001_platform_foundation
Revises:
Create Date: 2026-07-22
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0001_platform_foundation"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "blobs",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("sha256", sa.String(length=64), nullable=False),
        sa.Column("size_bytes", sa.BigInteger(), nullable=False),
        sa.Column("media_type", sa.String(length=255), nullable=True),
        sa.Column("bucket", sa.String(length=255), nullable=False),
        sa.Column("object_key", sa.Text(), nullable=False),
        sa.Column("etag", sa.String(length=255), nullable=True),
        sa.Column("state", sa.String(length=32), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("expired_at", sa.DateTime(timezone=True), nullable=True),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("object_key"),
        sa.UniqueConstraint("sha256"),
    )
    op.create_index("ix_blobs_state_expires_at", "blobs", ["state", "expires_at"], unique=False)

    op.create_table(
        "assets",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("blob_id", sa.Uuid(), nullable=False),
        sa.Column("source_filename", sa.Text(), nullable=False),
        sa.Column("source_kind", sa.String(length=64), nullable=False),
        sa.Column("source_uri", sa.Text(), nullable=True),
        sa.Column("source_metadata", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["blob_id"], ["blobs.id"], ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_assets_blob_id", "assets", ["blob_id"], unique=False)

    op.create_table(
        "pipeline_runs",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("source_blob_id", sa.Uuid(), nullable=False),
        sa.Column("run_key", sa.String(length=64), nullable=False),
        sa.Column("pipeline_name", sa.String(length=128), nullable=False),
        sa.Column("pipeline_version", sa.String(length=64), nullable=False),
        sa.Column("schema_version", sa.String(length=64), nullable=False),
        sa.Column("options", sa.JSON(), nullable=False),
        sa.Column("processor_versions", sa.JSON(), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["source_blob_id"], ["blobs.id"], ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("run_key"),
    )
    op.create_index("ix_pipeline_runs_source_blob_id", "pipeline_runs", ["source_blob_id"], unique=False)
    op.create_index("ix_pipeline_runs_status_created_at", "pipeline_runs", ["status", "created_at"], unique=False)

    op.create_table(
        "artifacts",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("pipeline_run_id", sa.Uuid(), nullable=False),
        sa.Column("blob_id", sa.Uuid(), nullable=False),
        sa.Column("artifact_type", sa.String(length=128), nullable=False),
        sa.Column("format", sa.String(length=64), nullable=False),
        sa.Column("schema_version", sa.String(length=64), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["blob_id"], ["blobs.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["pipeline_run_id"], ["pipeline_runs.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("pipeline_run_id", "artifact_type", name="uq_artifact_run_type"),
    )
    op.create_index("ix_artifacts_blob_id", "artifacts", ["blob_id"], unique=False)
    op.create_index("ix_artifacts_pipeline_run_id", "artifacts", ["pipeline_run_id"], unique=False)

    op.create_table(
        "job_requests",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("pipeline_run_id", sa.Uuid(), nullable=False),
        sa.Column("asset_id", sa.Uuid(), nullable=False),
        sa.Column("client_job_id", sa.String(length=255), nullable=True),
        sa.Column("idempotency_key", sa.String(length=255), nullable=True),
        sa.Column("callback_url", sa.Text(), nullable=True),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("cache_hit", sa.Boolean(), nullable=False),
        sa.Column("run_reused", sa.Boolean(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.ForeignKeyConstraint(["asset_id"], ["assets.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["pipeline_run_id"], ["pipeline_runs.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("idempotency_key"),
    )
    op.create_index("ix_job_requests_asset_id", "job_requests", ["asset_id"], unique=False)
    op.create_index("ix_job_requests_client_job_id", "job_requests", ["client_job_id"], unique=False)
    op.create_index("ix_job_requests_pipeline_run_id", "job_requests", ["pipeline_run_id"], unique=False)


def downgrade() -> None:
    op.drop_index("ix_job_requests_pipeline_run_id", table_name="job_requests")
    op.drop_index("ix_job_requests_client_job_id", table_name="job_requests")
    op.drop_index("ix_job_requests_asset_id", table_name="job_requests")
    op.drop_table("job_requests")
    op.drop_index("ix_artifacts_pipeline_run_id", table_name="artifacts")
    op.drop_index("ix_artifacts_blob_id", table_name="artifacts")
    op.drop_table("artifacts")
    op.drop_index("ix_pipeline_runs_status_created_at", table_name="pipeline_runs")
    op.drop_index("ix_pipeline_runs_source_blob_id", table_name="pipeline_runs")
    op.drop_table("pipeline_runs")
    op.drop_index("ix_assets_blob_id", table_name="assets")
    op.drop_table("assets")
    op.drop_index("ix_blobs_state_expires_at", table_name="blobs")
    op.drop_table("blobs")
