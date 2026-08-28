"""Add plan revision history and an explicit workflow handoff request."""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "20260826_0009"
down_revision: str | None = "20260826_0008"
branch_labels: Sequence[str] | None = None
depends_on: Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "plan_drafts",
        sa.Column("revision", sa.Integer(), nullable=False, server_default="1"),
        schema="dx_governance",
    )
    op.create_check_constraint(
        "ck_plan_draft_revision_positive",
        "plan_drafts",
        "revision >= 1",
        schema="dx_governance",
    )
    op.create_table(
        "plan_draft_revisions",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("draft_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("revision", sa.Integer(), nullable=False),
        sa.Column("steps", postgresql.JSONB(), nullable=False),
        sa.Column("reason", sa.Text(), nullable=False),
        sa.Column("actor_type", sa.String(32), nullable=False),
        sa.Column("actor_id", sa.String(128), nullable=False),
        sa.Column("idempotency_key", sa.String(128), nullable=False),
        sa.Column("request_hash", sa.String(64), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.ForeignKeyConstraint(["draft_id"], ["dx_governance.plan_drafts.id"]),
        sa.UniqueConstraint("draft_id", "revision", name="uq_plan_draft_revision"),
        sa.UniqueConstraint(
            "actor_type",
            "actor_id",
            "idempotency_key",
            name="uq_plan_draft_revision_idempotency",
        ),
        sa.CheckConstraint("revision >= 1", name="ck_plan_draft_revision_number_positive"),
        schema="dx_governance",
    )
    op.create_index(
        "ix_plan_draft_revisions_draft_created",
        "plan_draft_revisions",
        ["draft_id", "created_at"],
        schema="dx_governance",
    )
    op.create_table(
        "workflow_handoffs",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("task_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("proposal_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("draft_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("draft_revision", sa.Integer(), nullable=False),
        sa.Column("status", sa.String(16), nullable=False, server_default="requested"),
        sa.Column("actor_type", sa.String(32), nullable=False),
        sa.Column("actor_id", sa.String(128), nullable=False),
        sa.Column("idempotency_key", sa.String(128), nullable=False),
        sa.Column("request_hash", sa.String(64), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.ForeignKeyConstraint(["task_id"], ["dx_core.tasks.id"]),
        sa.ForeignKeyConstraint(["proposal_id"], ["dx_governance.intent_proposals.id"]),
        sa.ForeignKeyConstraint(["draft_id"], ["dx_governance.plan_drafts.id"]),
        sa.UniqueConstraint(
            "actor_type", "actor_id", "idempotency_key", name="uq_workflow_handoff_idempotency"
        ),
        sa.UniqueConstraint(
            "draft_id", "draft_revision", name="uq_workflow_handoff_draft_revision"
        ),
        sa.CheckConstraint(
            "status IN ('requested', 'accepted', 'cancelled')",
            name="ck_workflow_handoff_status",
        ),
        sa.CheckConstraint("draft_revision >= 1", name="ck_workflow_handoff_revision_positive"),
        schema="dx_governance",
    )
    op.create_index(
        "ix_workflow_handoffs_proposal_created",
        "workflow_handoffs",
        ["proposal_id", "created_at"],
        schema="dx_governance",
    )
    op.execute(
        sa.text(
            "GRANT SELECT, INSERT ON dx_governance.plan_draft_revisions, "
            "dx_governance.workflow_handoffs TO dx_api"
        )
    )
    op.execute(
        sa.text(
            "GRANT SELECT ON dx_governance.plan_draft_revisions, "
            "dx_governance.workflow_handoffs TO dx_worker"
        )
    )


def downgrade() -> None:
    op.execute(
        sa.text(
            "REVOKE SELECT ON dx_governance.plan_draft_revisions, "
            "dx_governance.workflow_handoffs FROM dx_worker"
        )
    )
    op.execute(
        sa.text(
            "REVOKE SELECT, INSERT ON dx_governance.plan_draft_revisions, "
            "dx_governance.workflow_handoffs FROM dx_api"
        )
    )
    op.drop_index(
        "ix_workflow_handoffs_proposal_created",
        table_name="workflow_handoffs",
        schema="dx_governance",
    )
    op.drop_table("workflow_handoffs", schema="dx_governance")
    op.drop_index(
        "ix_plan_draft_revisions_draft_created",
        table_name="plan_draft_revisions",
        schema="dx_governance",
    )
    op.drop_table("plan_draft_revisions", schema="dx_governance")
    op.drop_constraint(
        "ck_plan_draft_revision_positive", "plan_drafts", schema="dx_governance", type_="check"
    )
    op.drop_column("plan_drafts", "revision", schema="dx_governance")
