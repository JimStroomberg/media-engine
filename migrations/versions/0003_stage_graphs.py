"""Add durable multi-stage dependencies and artifact provenance.

Revision ID: 0003_stage_graphs
Revises: 0002_worker_leases
Create Date: 2026-07-22
"""

import uuid
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0003_stage_graphs"
down_revision: str | None = "0002_worker_leases"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "stage_dependencies",
        sa.Column("stage_run_id", sa.Uuid(), nullable=False),
        sa.Column("depends_on_stage_run_id", sa.Uuid(), nullable=False),
        sa.ForeignKeyConstraint(["depends_on_stage_run_id"], ["stage_runs.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["stage_run_id"], ["stage_runs.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("stage_run_id", "depends_on_stage_run_id"),
        sa.CheckConstraint("stage_run_id <> depends_on_stage_run_id", name="ck_stage_dependency_not_self"),
    )
    op.create_index(
        "ix_stage_dependencies_depends_on_stage_run_id",
        "stage_dependencies",
        ["depends_on_stage_run_id"],
        unique=False,
    )

    op.add_column("artifacts", sa.Column("producer_stage_run_id", sa.Uuid(), nullable=True))
    op.create_foreign_key(
        "fk_artifacts_producer_stage_run_id_stage_runs",
        "artifacts",
        "stage_runs",
        ["producer_stage_run_id"],
        ["id"],
        ondelete="CASCADE",
    )

    # Development installations may have completed foundation-era artifacts
    # from before durable workers existed. Give each such run one completed
    # producer stage so the provenance column can become non-null safely.
    connection = op.get_bind()
    missing_stage_runs = connection.execute(
        sa.text(
            """
            SELECT DISTINCT pipeline_runs.id, pipeline_runs.pipeline_name
            FROM pipeline_runs
            JOIN artifacts ON artifacts.pipeline_run_id = pipeline_runs.id
            WHERE NOT EXISTS (
                SELECT 1 FROM stage_runs WHERE stage_runs.pipeline_run_id = pipeline_runs.id
            )
            """
        )
    )
    for pipeline_run_id, pipeline_name in missing_stage_runs:
        connection.execute(
            sa.text(
                """
                INSERT INTO stage_runs (
                    id, pipeline_run_id, stage_name, status, required_capabilities,
                    priority, attempt, max_attempts, progress, completed_at
                ) VALUES (
                    :id, :pipeline_run_id, :stage_name, 'completed', CAST('{}' AS JSON),
                    0, 1, 3, 1.0, now()
                )
                """
            ),
            {
                "id": uuid.uuid4(),
                "pipeline_run_id": pipeline_run_id,
                "stage_name": pipeline_name,
            },
        )
    op.execute(
        """
        UPDATE artifacts AS artifact
        SET producer_stage_run_id = (
            SELECT id
            FROM stage_runs
            WHERE pipeline_run_id = artifact.pipeline_run_id
            ORDER BY created_at, id
            LIMIT 1
        )
        """
    )
    op.alter_column("artifacts", "producer_stage_run_id", nullable=False)
    op.create_index(
        "ix_artifacts_producer_stage_run_id",
        "artifacts",
        ["producer_stage_run_id"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index("ix_artifacts_producer_stage_run_id", table_name="artifacts")
    op.drop_constraint("fk_artifacts_producer_stage_run_id_stage_runs", "artifacts", type_="foreignkey")
    op.drop_column("artifacts", "producer_stage_run_id")
    op.drop_index("ix_stage_dependencies_depends_on_stage_run_id", table_name="stage_dependencies")
    op.drop_table("stage_dependencies")
