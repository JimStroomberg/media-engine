"""Add removable worker audit records.

Revision ID: 0007_worker_removal
Revises: 0006_managed_workers
Create Date: 2026-08-06
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0007_worker_removal"
down_revision: str | None = "0006_managed_workers"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("workers", sa.Column("removed_at", sa.DateTime(timezone=True), nullable=True))
    op.create_index("ix_workers_removed_at", "workers", ["removed_at"], unique=False)


def downgrade() -> None:
    op.drop_index("ix_workers_removed_at", table_name="workers")
    op.drop_column("workers", "removed_at")
