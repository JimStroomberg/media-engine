"""Add individually managed worker credentials and lifecycle state.

Revision ID: 0006_managed_workers
Revises: 0005_webhook_outbox
Create Date: 2026-08-06
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0006_managed_workers"
down_revision: str | None = "0005_webhook_outbox"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("workers", sa.Column("profile", sa.String(length=64), nullable=False, server_default="cpu"))
    op.add_column("workers", sa.Column("runtime", sa.JSON(), nullable=False, server_default=sa.text("'{}'::json")))
    op.add_column(
        "workers",
        sa.Column("desired_state", sa.String(length=32), nullable=False, server_default="active"),
    )
    op.add_column("workers", sa.Column("credential_prefix", sa.String(length=32), nullable=True))
    op.add_column("workers", sa.Column("credential_hash", sa.String(length=64), nullable=True))
    op.add_column("workers", sa.Column("credential_created_at", sa.DateTime(timezone=True), nullable=True))
    op.add_column("workers", sa.Column("credential_expires_at", sa.DateTime(timezone=True), nullable=True))
    op.add_column("workers", sa.Column("credential_last_used_at", sa.DateTime(timezone=True), nullable=True))
    op.add_column("workers", sa.Column("credential_revoked_at", sa.DateTime(timezone=True), nullable=True))
    op.add_column(
        "workers",
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
    )
    op.alter_column("workers", "registered_at", existing_type=sa.DateTime(timezone=True), nullable=True)
    op.alter_column("workers", "last_seen_at", existing_type=sa.DateTime(timezone=True), nullable=True)
    op.create_index("uq_workers_credential_prefix", "workers", ["credential_prefix"], unique=True)
    op.create_index("uq_workers_credential_hash", "workers", ["credential_hash"], unique=True)

    # Legacy worker rows have no individually attributable credential and must be explicitly re-enrolled.
    op.execute("UPDATE workers SET desired_state = 'revoked', status = 'offline'")


def downgrade() -> None:
    op.drop_index("uq_workers_credential_hash", table_name="workers")
    op.drop_index("uq_workers_credential_prefix", table_name="workers")
    op.execute(
        "UPDATE workers SET registered_at = COALESCE(registered_at, created_at), "
        "last_seen_at = COALESCE(last_seen_at, created_at)"
    )
    op.alter_column("workers", "last_seen_at", existing_type=sa.DateTime(timezone=True), nullable=False)
    op.alter_column("workers", "registered_at", existing_type=sa.DateTime(timezone=True), nullable=False)
    op.drop_column("workers", "created_at")
    op.drop_column("workers", "credential_revoked_at")
    op.drop_column("workers", "credential_last_used_at")
    op.drop_column("workers", "credential_expires_at")
    op.drop_column("workers", "credential_created_at")
    op.drop_column("workers", "credential_hash")
    op.drop_column("workers", "credential_prefix")
    op.drop_column("workers", "desired_state")
    op.drop_column("workers", "runtime")
    op.drop_column("workers", "profile")
