"""Create agent_executions persistence for the Agent Execution Bridge (V0.2).

Phase-1 migration only: one table in the existing dx_workflow schema plus the
dx_api/dx_worker grants. No workspace registry, provider registry, artifact store,
retry queue, or ExecutionReport linkage is introduced here (those are deferred).
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "20260921_0013"
down_revision: str | None = "20260826_0012"
branch_labels: Sequence[str] | None = None
depends_on: Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "agent_executions",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("task_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("proposal_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("approval_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("workflow_run_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("workflow_step_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("draft_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("draft_revision", sa.Integer(), nullable=False),
        sa.Column("agent_role", sa.String(64), nullable=False),
        sa.Column("agent_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("provider", sa.String(32), nullable=False),
        sa.Column("capability", sa.String(128), nullable=False),
        sa.Column("workspace_ref", sa.String(64), nullable=True),
        sa.Column("status", sa.String(16), nullable=False, server_default="requested"),
        sa.Column("request_input", postgresql.JSONB(), nullable=False),
        sa.Column("constraints", postgresql.JSONB(), nullable=True),
        sa.Column("external_execution_id", sa.String(255), nullable=True),
        sa.Column("summary", sa.Text(), nullable=True),
        sa.Column("artifacts", postgresql.JSONB(), nullable=True),
        sa.Column("log_references", postgresql.JSONB(), nullable=True),
        sa.Column("result_tail", sa.Text(), nullable=True),
        sa.Column("exit_code", sa.Integer(), nullable=True),
        sa.Column("error_code", sa.String(64), nullable=True),
        sa.Column("error_detail", sa.Text(), nullable=True),
        sa.Column("idempotency_key", sa.String(128), nullable=False),
        sa.Column("request_hash", sa.String(64), nullable=False),
        sa.Column("actor_type", sa.String(32), nullable=False),
        sa.Column("actor_id", sa.String(128), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.Column("dispatched_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.ForeignKeyConstraint(["task_id"], ["dx_core.tasks.id"]),
        sa.ForeignKeyConstraint(["proposal_id"], ["dx_governance.intent_proposals.id"]),
        sa.ForeignKeyConstraint(["approval_id"], ["dx_governance.approval_grants.id"]),
        sa.ForeignKeyConstraint(["workflow_run_id"], ["dx_workflow.workflow_runs.id"]),
        sa.ForeignKeyConstraint(["workflow_step_id"], ["dx_workflow.workflow_steps.id"]),
        sa.ForeignKeyConstraint(["draft_id"], ["dx_governance.plan_drafts.id"]),
        sa.UniqueConstraint(
            "actor_type", "actor_id", "idempotency_key", name="uq_agent_execution_idempotency"
        ),
        sa.CheckConstraint(
            "status IN ('requested', 'dispatched', 'running', 'succeeded', 'failed')",
            name="ck_agent_execution_status",
        ),
        sa.CheckConstraint("draft_revision >= 1", name="ck_agent_execution_revision_positive"),
        sa.CheckConstraint(
            "workspace_ref IS NULL OR workspace_ref ~ '^[a-z0-9][a-z0-9_-]*$'",
            name="ck_agent_execution_workspace_ref_symbolic",
        ),
        schema="dx_workflow",
    )
    op.create_index(
        "ix_agent_executions_task_created",
        "agent_executions",
        ["task_id", "created_at"],
        schema="dx_workflow",
    )
    op.create_index(
        "ix_agent_executions_step_status",
        "agent_executions",
        ["workflow_step_id", "status"],
        schema="dx_workflow",
    )
    op.create_index(
        "ix_agent_executions_proposal_created",
        "agent_executions",
        ["proposal_id", "created_at"],
        schema="dx_workflow",
    )

    # dx_workflow USAGE already granted in migration 0010; grant table-level per the
    # established dx_api (write) / dx_worker (read-only) split.
    op.execute(
        sa.text("GRANT SELECT, INSERT, UPDATE ON dx_workflow.agent_executions TO dx_api")
    )
    op.execute(sa.text("GRANT SELECT ON dx_workflow.agent_executions TO dx_worker"))


def downgrade() -> None:
    op.execute(
        sa.text("REVOKE SELECT ON dx_workflow.agent_executions FROM dx_worker")
    )
    op.execute(
        sa.text(
            "REVOKE SELECT, INSERT, UPDATE ON dx_workflow.agent_executions FROM dx_api"
        )
    )
    op.drop_index(
        "ix_agent_executions_proposal_created", table_name="agent_executions", schema="dx_workflow"
    )
    op.drop_index(
        "ix_agent_executions_step_status", table_name="agent_executions", schema="dx_workflow"
    )
    op.drop_index(
        "ix_agent_executions_task_created", table_name="agent_executions", schema="dx_workflow"
    )
    op.drop_table("agent_executions", schema="dx_workflow")
