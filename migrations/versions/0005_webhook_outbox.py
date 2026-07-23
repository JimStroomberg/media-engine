"""Add client webhook endpoints and durable delivery outbox.

Revision ID: 0005_webhook_outbox
Revises: 0004_access_providers_usage
Create Date: 2026-07-22
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0005_webhook_outbox"
down_revision: str | None = "0004_access_providers_usage"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "webhook_endpoints",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("client_id", sa.Uuid(), nullable=False),
        sa.Column("name", sa.String(length=255), nullable=False),
        sa.Column("url", sa.Text(), nullable=False),
        sa.Column("encrypted_signing_secret", sa.Text(), nullable=False),
        sa.Column("signing_secret_hint", sa.String(length=64), nullable=False),
        sa.Column("events", sa.JSON(), nullable=False),
        sa.Column("enabled", sa.Boolean(), nullable=False),
        sa.Column("is_default", sa.Boolean(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.ForeignKeyConstraint(["client_id"], ["clients.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("client_id", "name", name="uq_webhook_endpoint_client_name"),
    )
    op.create_index("ix_webhook_endpoints_client_id", "webhook_endpoints", ["client_id"], unique=False)
    op.create_index(
        "uq_webhook_endpoints_client_default",
        "webhook_endpoints",
        ["client_id"],
        unique=True,
        postgresql_where=sa.text("is_default"),
    )

    op.add_column("job_requests", sa.Column("webhook_endpoint_id", sa.Uuid(), nullable=True))
    op.create_foreign_key(
        "fk_job_requests_webhook_endpoint_id",
        "job_requests",
        "webhook_endpoints",
        ["webhook_endpoint_id"],
        ["id"],
        ondelete="SET NULL",
    )
    op.create_index(
        "ix_job_requests_webhook_endpoint_id",
        "job_requests",
        ["webhook_endpoint_id"],
        unique=False,
    )

    op.create_table(
        "webhook_events",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("client_id", sa.Uuid(), nullable=False),
        sa.Column("endpoint_id", sa.Uuid(), nullable=False),
        sa.Column("job_request_id", sa.Uuid(), nullable=True),
        sa.Column("event_type", sa.String(length=64), nullable=False),
        sa.Column("payload", sa.JSON(), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("attempt_count", sa.Integer(), nullable=False),
        sa.Column("next_attempt_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("lease_token", sa.Uuid(), nullable=True),
        sa.Column("lease_expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("delivered_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("abandoned_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_error", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.ForeignKeyConstraint(["client_id"], ["clients.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["endpoint_id"], ["webhook_endpoints.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["job_request_id"], ["job_requests.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("job_request_id", "event_type", name="uq_webhook_event_job_type"),
        sa.UniqueConstraint("lease_token"),
    )
    op.create_index("ix_webhook_events_client_id", "webhook_events", ["client_id"], unique=False)
    op.create_index("ix_webhook_events_endpoint_id", "webhook_events", ["endpoint_id"], unique=False)
    op.create_index("ix_webhook_events_job_request_id", "webhook_events", ["job_request_id"], unique=False)
    op.create_index("ix_webhook_events_dispatch", "webhook_events", ["status", "next_attempt_at"], unique=False)

    op.create_table(
        "webhook_delivery_attempts",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("webhook_event_id", sa.Uuid(), nullable=False),
        sa.Column("attempt_number", sa.Integer(), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("duration_ms", sa.Integer(), nullable=False),
        sa.Column("response_status", sa.Integer(), nullable=True),
        sa.Column("response_body_preview", sa.Text(), nullable=True),
        sa.Column("error_message", sa.Text(), nullable=True),
        sa.Column("outcome", sa.String(length=32), nullable=False),
        sa.ForeignKeyConstraint(["webhook_event_id"], ["webhook_events.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("webhook_event_id", "attempt_number", name="uq_webhook_delivery_event_attempt"),
    )
    op.create_index(
        "ix_webhook_delivery_attempts_webhook_event_id",
        "webhook_delivery_attempts",
        ["webhook_event_id"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index("ix_webhook_delivery_attempts_webhook_event_id", table_name="webhook_delivery_attempts")
    op.drop_table("webhook_delivery_attempts")
    op.drop_index("ix_webhook_events_dispatch", table_name="webhook_events")
    op.drop_index("ix_webhook_events_job_request_id", table_name="webhook_events")
    op.drop_index("ix_webhook_events_endpoint_id", table_name="webhook_events")
    op.drop_index("ix_webhook_events_client_id", table_name="webhook_events")
    op.drop_table("webhook_events")
    op.drop_index("ix_job_requests_webhook_endpoint_id", table_name="job_requests")
    op.drop_constraint("fk_job_requests_webhook_endpoint_id", "job_requests", type_="foreignkey")
    op.drop_column("job_requests", "webhook_endpoint_id")
    op.drop_index("uq_webhook_endpoints_client_default", table_name="webhook_endpoints")
    op.drop_index("ix_webhook_endpoints_client_id", table_name="webhook_endpoints")
    op.drop_table("webhook_endpoints")
