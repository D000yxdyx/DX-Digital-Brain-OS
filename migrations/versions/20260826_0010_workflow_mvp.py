"""Add the contract-only sequential Workflow MVP."""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "20260826_0010"
down_revision: str | None = "20260826_0009"
branch_labels: Sequence[str] | None = None
depends_on: Sequence[str] | None = None


def upgrade() -> None:
    op.execute(sa.text("CREATE SCHEMA dx_workflow"))
    op.create_table(
        "workflow_runs",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("task_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("proposal_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("handoff_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("draft_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("draft_revision", sa.Integer(), nullable=False),
        sa.Column("status", sa.String(16), nullable=False, server_default="ready"),
        sa.Column("actor_type", sa.String(32), nullable=False),
        sa.Column("actor_id", sa.String(128), nullable=False),
        sa.Column("idempotency_key", sa.String(128), nullable=False),
        sa.Column("request_hash", sa.String(64), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(["task_id"], ["dx_core.tasks.id"]),
        sa.ForeignKeyConstraint(["proposal_id"], ["dx_governance.intent_proposals.id"]),
        sa.ForeignKeyConstraint(["handoff_id"], ["dx_governance.workflow_handoffs.id"]),
        sa.ForeignKeyConstraint(["draft_id"], ["dx_governance.plan_drafts.id"]),
        sa.UniqueConstraint("handoff_id", name="uq_workflow_run_handoff"),
        sa.UniqueConstraint(
            "actor_type", "actor_id", "idempotency_key", name="uq_workflow_run_idempotency"
        ),
        sa.CheckConstraint(
            "status IN ('ready', 'running', 'paused', 'completed', 'cancelled', 'failed')",
            name="ck_workflow_run_status",
        ),
        sa.CheckConstraint("draft_revision >= 1", name="ck_workflow_run_revision_positive"),
        schema="dx_workflow",
    )
    op.create_index(
        "ix_workflow_runs_proposal_created",
        "workflow_runs",
        ["proposal_id", "created_at"],
        schema="dx_workflow",
    )
    op.create_table(
        "workflow_steps",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("run_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("ordinal", sa.Integer(), nullable=False),
        sa.Column("title", sa.String(500), nullable=False),
        sa.Column("capability", sa.String(128), nullable=False),
        sa.Column("parameters", postgresql.JSONB(), nullable=False),
        sa.Column("status", sa.String(16), nullable=False, server_default="pending"),
        sa.Column("result_summary", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.ForeignKeyConstraint(["run_id"], ["dx_workflow.workflow_runs.id"]),
        sa.UniqueConstraint("run_id", "ordinal", name="uq_workflow_step_ordinal"),
        sa.CheckConstraint(
            "status IN ('pending', 'running', 'succeeded', 'failed', 'cancelled')",
            name="ck_workflow_step_status",
        ),
        sa.CheckConstraint("ordinal >= 1", name="ck_workflow_step_ordinal_positive"),
        schema="dx_workflow",
    )
    op.create_index(
        "ix_workflow_steps_run_ordinal",
        "workflow_steps",
        ["run_id", "ordinal"],
        schema="dx_workflow",
    )
    op.create_table(
        "workflow_commands",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("run_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("operation", sa.String(64), nullable=False),
        sa.Column("actor_type", sa.String(32), nullable=False),
        sa.Column("actor_id", sa.String(128), nullable=False),
        sa.Column("idempotency_key", sa.String(128), nullable=False),
        sa.Column("request_hash", sa.String(64), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.ForeignKeyConstraint(["run_id"], ["dx_workflow.workflow_runs.id"]),
        sa.UniqueConstraint(
            "run_id", "actor_type", "actor_id", "idempotency_key",
            name="uq_workflow_command_idempotency",
        ),
        schema="dx_workflow",
    )
    op.execute(sa.text("GRANT USAGE ON SCHEMA dx_workflow TO dx_api, dx_worker"))
    op.execute(
        sa.text(
            "GRANT SELECT, INSERT, UPDATE ON dx_workflow.workflow_runs, "
            "dx_workflow.workflow_steps, dx_workflow.workflow_commands TO dx_api"
        )
    )
    op.execute(
        sa.text(
            "GRANT SELECT ON dx_workflow.workflow_runs, dx_workflow.workflow_steps, "
            "dx_workflow.workflow_commands TO dx_worker"
        )
    )


def downgrade() -> None:
    op.execute(
        sa.text(
            "REVOKE SELECT ON dx_workflow.workflow_runs, dx_workflow.workflow_steps, "
            "dx_workflow.workflow_commands FROM dx_worker"
        )
    )
    op.execute(
        sa.text(
            "REVOKE SELECT, INSERT, UPDATE ON dx_workflow.workflow_runs, "
            "dx_workflow.workflow_steps, dx_workflow.workflow_commands FROM dx_api"
        )
    )
    for name in (
        "workflow_commands",
        "workflow_steps",
        "workflow_runs",
    ):
        op.drop_table(name, schema="dx_workflow")
    op.execute(sa.text("DROP SCHEMA dx_workflow"))
