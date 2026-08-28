"""Add project dossiers, retrospective drafts, and candidate lessons."""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "20260826_0005"
down_revision: str | None = "20260826_0004"
branch_labels: Sequence[str] | None = None
depends_on: Sequence[str] | None = None


def _timestamp(name: str = "created_at") -> sa.Column:
    return sa.Column(
        name, sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False
    )


def upgrade() -> None:
    op.execute(sa.text("CREATE SCHEMA dx_archive"))
    op.create_table(
        "project_dossiers",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("task_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("status", sa.String(16), nullable=False, server_default="closed"),
        sa.Column("objective", sa.Text(), nullable=False),
        sa.Column("plan_snapshot", postgresql.JSONB(), nullable=False),
        sa.Column("result_summary", sa.Text(), nullable=True),
        sa.Column("artifacts", postgresql.JSONB(), nullable=False),
        sa.Column("evidence", postgresql.JSONB(), nullable=False),
        sa.Column("actor_type", sa.String(32), nullable=False),
        sa.Column("actor_id", sa.String(128), nullable=False),
        sa.Column("idempotency_key", sa.String(128), nullable=False),
        sa.Column("request_hash", sa.String(64), nullable=False),
        _timestamp(),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("closed_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(["task_id"], ["dx_core.tasks.id"]),
        sa.UniqueConstraint("task_id", name="uq_dossier_task"),
        sa.UniqueConstraint(
            "actor_type", "actor_id", "idempotency_key", name="uq_dossier_idempotency"
        ),
        sa.CheckConstraint("status IN ('open', 'closed', 'cancelled')", name="ck_dossier_status"),
        schema="dx_archive",
    )
    op.create_index("ix_dossier_task", "project_dossiers", ["task_id"], schema="dx_archive")

    op.create_table(
        "retrospectives",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("dossier_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("task_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("status", sa.String(16), nullable=False, server_default="draft"),
        sa.Column("expected_outcome", sa.Text(), nullable=False),
        sa.Column("actual_outcome", sa.Text(), nullable=False),
        sa.Column("deviations", postgresql.JSONB(), nullable=False),
        sa.Column("root_causes", postgresql.JSONB(), nullable=False),
        sa.Column("keep_items", postgresql.JSONB(), nullable=False),
        sa.Column("change_items", postgresql.JSONB(), nullable=False),
        sa.Column("stop_items", postgresql.JSONB(), nullable=False),
        sa.Column("follow_up_items", postgresql.JSONB(), nullable=False),
        sa.Column("evidence_refs", postgresql.JSONB(), nullable=False),
        sa.Column("actor_type", sa.String(32), nullable=False),
        sa.Column("actor_id", sa.String(128), nullable=False),
        sa.Column("idempotency_key", sa.String(128), nullable=False),
        sa.Column("request_hash", sa.String(64), nullable=False),
        _timestamp(),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(["dossier_id"], ["dx_archive.project_dossiers.id"]),
        sa.ForeignKeyConstraint(["task_id"], ["dx_core.tasks.id"]),
        sa.UniqueConstraint(
            "dossier_id", "actor_type", "actor_id", "idempotency_key", name="uq_retro_idempotency"
        ),
        sa.CheckConstraint(
            "status IN ('draft', 'validated', 'retracted')", name="ck_retrospective_status"
        ),
        schema="dx_archive",
    )
    op.create_index(
        "ix_retro_dossier", "retrospectives", ["dossier_id", "created_at"], schema="dx_archive"
    )

    op.create_table(
        "lessons",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("retrospective_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("task_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("status", sa.String(16), nullable=False, server_default="candidate"),
        sa.Column("memory_type", sa.String(16), nullable=False),
        sa.Column("statement", sa.Text(), nullable=False),
        sa.Column("applicability", postgresql.JSONB(), nullable=False),
        sa.Column("evidence_refs", postgresql.JSONB(), nullable=False),
        sa.Column("confidence", sa.Float(), nullable=False),
        sa.Column("actor_type", sa.String(32), nullable=False),
        sa.Column("actor_id", sa.String(128), nullable=False),
        sa.Column("idempotency_key", sa.String(128), nullable=False),
        sa.Column("request_hash", sa.String(64), nullable=False),
        _timestamp(),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("reviewed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("reviewer_actor_type", sa.String(32), nullable=True),
        sa.Column("reviewer_actor_id", sa.String(128), nullable=True),
        sa.Column("review_reason", sa.Text(), nullable=True),
        sa.Column("review_idempotency_key", sa.String(128), nullable=True),
        sa.Column("review_request_hash", sa.String(64), nullable=True),
        sa.ForeignKeyConstraint(["retrospective_id"], ["dx_archive.retrospectives.id"]),
        sa.ForeignKeyConstraint(["task_id"], ["dx_core.tasks.id"]),
        sa.UniqueConstraint(
            "retrospective_id",
            "actor_type",
            "actor_id",
            "idempotency_key",
            name="uq_lesson_idempotency",
        ),
        sa.CheckConstraint(
            "status IN ('candidate', 'validated', 'superseded', 'retracted')",
            name="ck_lesson_status",
        ),
        sa.CheckConstraint(
            "memory_type IN ('episodic', 'semantic', 'procedural')", name="ck_lesson_memory_type"
        ),
        sa.CheckConstraint("confidence >= 0 AND confidence <= 1", name="ck_lesson_confidence"),
        schema="dx_archive",
    )
    op.create_index(
        "ix_lesson_retro", "lessons", ["retrospective_id", "created_at"], schema="dx_archive"
    )

    op.execute("GRANT USAGE ON SCHEMA dx_archive TO dx_api, dx_worker")
    op.execute("GRANT SELECT, INSERT, UPDATE ON dx_archive.project_dossiers TO dx_api")
    op.execute(
        "GRANT SELECT, INSERT, UPDATE ON dx_archive.retrospectives, dx_archive.lessons TO dx_api"
    )
    op.execute(
        "GRANT SELECT ON dx_archive.project_dossiers, dx_archive.retrospectives, "
        "dx_archive.lessons TO dx_worker"
    )


def downgrade() -> None:
    for table, index in (
        ("lessons", "ix_lesson_retro"),
        ("retrospectives", "ix_retro_dossier"),
        ("project_dossiers", "ix_dossier_task"),
    ):
        op.drop_index(index, table_name=table, schema="dx_archive")
        op.drop_table(table, schema="dx_archive")
    op.execute(sa.text("DROP SCHEMA dx_archive"))
