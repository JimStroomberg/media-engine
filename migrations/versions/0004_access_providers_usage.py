"""Add clients, API keys, provider configuration, and AI usage accounting.

Revision ID: 0004_access_providers_usage
Revises: 0003_stage_graphs
Create Date: 2026-07-22
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0004_access_providers_usage"
down_revision: str | None = "0003_stage_graphs"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

DEFAULT_CLIENT_ID = "00000000-0000-0000-0000-000000000001"


def upgrade() -> None:
    op.create_table(
        "clients",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("name", sa.String(length=255), nullable=False),
        sa.Column("enabled", sa.Boolean(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("name"),
    )
    op.execute(
        sa.text(
            "INSERT INTO clients (id, name, enabled) VALUES "
            "(CAST(:client_id AS UUID), 'Migrated local client', true)"
        ).bindparams(client_id=DEFAULT_CLIENT_ID)
    )

    op.create_table(
        "provider_configs",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("name", sa.String(length=255), nullable=False),
        sa.Column("provider", sa.String(length=32), nullable=False),
        sa.Column("encrypted_api_key", sa.Text(), nullable=False),
        sa.Column("credential_hint", sa.String(length=64), nullable=False),
        sa.Column("base_url", sa.Text(), nullable=False),
        sa.Column("models", sa.JSON(), nullable=False),
        sa.Column("runtime_options", sa.JSON(), nullable=False),
        sa.Column("enabled", sa.Boolean(), nullable=False),
        sa.Column("is_default", sa.Boolean(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("last_verified_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_error", sa.Text(), nullable=True),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("name"),
        sa.UniqueConstraint("provider"),
    )
    op.create_index(
        "uq_provider_configs_default",
        "provider_configs",
        ["is_default"],
        unique=True,
        postgresql_where=sa.text("is_default"),
    )

    op.create_table(
        "api_keys",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("client_id", sa.Uuid(), nullable=False),
        sa.Column("name", sa.String(length=255), nullable=False),
        sa.Column("key_prefix", sa.String(length=32), nullable=False),
        sa.Column("key_hash", sa.String(length=64), nullable=False),
        sa.Column("scopes", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_used_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(["client_id"], ["clients.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("client_id", "name", name="uq_api_key_client_name"),
        sa.UniqueConstraint("key_hash"),
        sa.UniqueConstraint("key_prefix"),
    )
    op.create_index("ix_api_keys_client_id", "api_keys", ["client_id"], unique=False)

    op.add_column("assets", sa.Column("client_id", sa.Uuid(), nullable=True))
    op.create_foreign_key(
        "fk_assets_client_id_clients",
        "assets",
        "clients",
        ["client_id"],
        ["id"],
        ondelete="RESTRICT",
    )
    op.execute(f"UPDATE assets SET client_id = '{DEFAULT_CLIENT_ID}' WHERE client_id IS NULL")
    op.alter_column("assets", "client_id", nullable=False)
    op.create_index("ix_assets_client_id", "assets", ["client_id"], unique=False)

    op.add_column("job_requests", sa.Column("client_id", sa.Uuid(), nullable=True))
    op.create_foreign_key(
        "fk_job_requests_client_id_clients",
        "job_requests",
        "clients",
        ["client_id"],
        ["id"],
        ondelete="RESTRICT",
    )
    op.execute(f"UPDATE job_requests SET client_id = '{DEFAULT_CLIENT_ID}' WHERE client_id IS NULL")
    op.alter_column("job_requests", "client_id", nullable=False)
    op.create_index("ix_job_requests_client_id", "job_requests", ["client_id"], unique=False)
    op.drop_constraint("job_requests_idempotency_key_key", "job_requests", type_="unique")
    op.create_unique_constraint(
        "uq_job_request_client_idempotency",
        "job_requests",
        ["client_id", "idempotency_key"],
    )

    op.create_table(
        "ai_usage_events",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("stage_run_id", sa.Uuid(), nullable=False),
        sa.Column("provider_config_id", sa.Uuid(), nullable=True),
        sa.Column("stage_attempt", sa.Integer(), nullable=False),
        sa.Column("provider", sa.String(length=32), nullable=False),
        sa.Column("model", sa.String(length=128), nullable=False),
        sa.Column("operation", sa.String(length=64), nullable=False),
        sa.Column("outcome", sa.String(length=32), nullable=False),
        sa.Column("usage", sa.JSON(), nullable=False),
        sa.Column("cost_usd_ticks", sa.BigInteger(), nullable=True),
        sa.Column("estimated_cost_usd", sa.Numeric(precision=20, scale=10), nullable=True),
        sa.Column("latency_ms", sa.Integer(), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.ForeignKeyConstraint(["provider_config_id"], ["provider_configs.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["stage_run_id"], ["stage_runs.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_ai_usage_events_provider_config_id", "ai_usage_events", ["provider_config_id"], unique=False)
    op.create_index("ix_ai_usage_events_stage_run_id", "ai_usage_events", ["stage_run_id"], unique=False)
    op.create_index(
        "ix_ai_usage_events_provider_created_at",
        "ai_usage_events",
        ["provider", "created_at"],
        unique=False,
    )
    op.create_index(
        "ix_ai_usage_events_stage_attempt",
        "ai_usage_events",
        ["stage_run_id", "stage_attempt"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index("ix_ai_usage_events_stage_attempt", table_name="ai_usage_events")
    op.drop_index("ix_ai_usage_events_provider_created_at", table_name="ai_usage_events")
    op.drop_index("ix_ai_usage_events_stage_run_id", table_name="ai_usage_events")
    op.drop_index("ix_ai_usage_events_provider_config_id", table_name="ai_usage_events")
    op.drop_table("ai_usage_events")

    op.drop_constraint("uq_job_request_client_idempotency", "job_requests", type_="unique")
    op.create_unique_constraint("job_requests_idempotency_key_key", "job_requests", ["idempotency_key"])
    op.drop_index("ix_job_requests_client_id", table_name="job_requests")
    op.drop_constraint("fk_job_requests_client_id_clients", "job_requests", type_="foreignkey")
    op.drop_column("job_requests", "client_id")

    op.drop_index("ix_assets_client_id", table_name="assets")
    op.drop_constraint("fk_assets_client_id_clients", "assets", type_="foreignkey")
    op.drop_column("assets", "client_id")

    op.drop_index("ix_api_keys_client_id", table_name="api_keys")
    op.drop_table("api_keys")
    op.drop_index("uq_provider_configs_default", table_name="provider_configs")
    op.drop_table("provider_configs")
    op.drop_table("clients")
