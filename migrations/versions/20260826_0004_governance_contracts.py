"""Add durable governance contracts for plan, review, approval, and execution."""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "20260826_0004"
down_revision: str | None = "20260826_0003"
branch_labels: Sequence[str] | None = None
depends_on: Sequence[str] | None = None


def _timestamp() -> sa.Column:
    return sa.Column(
        "created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False
    )


def upgrade() -> None:
    op.execute(sa.text("CREATE SCHEMA dx_governance"))

    op.create_table(
        "intent_proposals",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("task_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("revision", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("status", sa.String(32), nullable=False, server_default="proposed"),
        sa.Column("objective", sa.String(240), nullable=False),
        sa.Column("rationale", sa.Text(), nullable=False),
        sa.Column("scope", postgresql.JSONB(), nullable=False),
        sa.Column("requested_capabilities", postgresql.JSONB(), nullable=False),
        sa.Column("plan", postgresql.JSONB(), nullable=False),
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
        sa.ForeignKeyConstraint(["task_id"], ["dx_core.tasks.id"]),
        sa.UniqueConstraint(
            "actor_type", "actor_id", "idempotency_key", name="uq_proposal_idempotency"
        ),
        sa.CheckConstraint("revision >= 1", name="ck_proposal_revision_positive"),
        sa.CheckConstraint(
            "status IN ("
            "'proposed', 'awaiting_input', 'approved', 'rejected', "
            "'executing', 'completed', 'failed', 'cancelled')",
            name="ck_proposal_status",
        ),
        schema="dx_governance",
    )
    op.create_index(
        "ix_proposal_task", "intent_proposals", ["task_id", "created_at"], schema="dx_governance"
    )

    op.create_table(
        "review_decisions",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("proposal_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("task_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("decision", sa.String(24), nullable=False),
        sa.Column("risk_level", sa.String(16), nullable=False),
        sa.Column("reason", sa.Text(), nullable=False),
        sa.Column("actor_type", sa.String(32), nullable=False),
        sa.Column("actor_id", sa.String(128), nullable=False),
        sa.Column("idempotency_key", sa.String(128), nullable=False),
        sa.Column("request_hash", sa.String(64), nullable=False),
        _timestamp(),
        sa.ForeignKeyConstraint(["proposal_id"], ["dx_governance.intent_proposals.id"]),
        sa.ForeignKeyConstraint(["task_id"], ["dx_core.tasks.id"]),
        sa.UniqueConstraint(
            "proposal_id", "actor_type", "actor_id", "idempotency_key", name="uq_review_idempotency"
        ),
        sa.CheckConstraint(
            "decision IN ('approve', 'reject', 'request_input')", name="ck_review_decision"
        ),
        sa.CheckConstraint("risk_level IN ('low', 'medium', 'high')", name="ck_review_risk_level"),
        schema="dx_governance",
    )
    op.create_index(
        "ix_review_proposal",
        "review_decisions",
        ["proposal_id", "created_at"],
        schema="dx_governance",
    )

    op.create_table(
        "approval_grants",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("proposal_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("task_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("scope", postgresql.JSONB(), nullable=False),
        sa.Column("status", sa.String(16), nullable=False, server_default="active"),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("actor_type", sa.String(32), nullable=False),
        sa.Column("actor_id", sa.String(128), nullable=False),
        sa.Column("idempotency_key", sa.String(128), nullable=False),
        sa.Column("request_hash", sa.String(64), nullable=False),
        _timestamp(),
        sa.ForeignKeyConstraint(["proposal_id"], ["dx_governance.intent_proposals.id"]),
        sa.ForeignKeyConstraint(["task_id"], ["dx_core.tasks.id"]),
        sa.UniqueConstraint(
            "proposal_id",
            "actor_type",
            "actor_id",
            "idempotency_key",
            name="uq_approval_idempotency",
        ),
        sa.CheckConstraint("status IN ('active', 'revoked')", name="ck_approval_status"),
        schema="dx_governance",
    )
    op.create_index(
        "ix_approval_proposal",
        "approval_grants",
        ["proposal_id", "created_at"],
        schema="dx_governance",
    )

    op.create_table(
        "execution_reports",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("proposal_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("task_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("summary", sa.Text(), nullable=False),
        sa.Column("artifacts", postgresql.JSONB(), nullable=False),
        sa.Column("actor_type", sa.String(32), nullable=False),
        sa.Column("actor_id", sa.String(128), nullable=False),
        sa.Column("idempotency_key", sa.String(128), nullable=False),
        sa.Column("request_hash", sa.String(64), nullable=False),
        _timestamp(),
        sa.ForeignKeyConstraint(["proposal_id"], ["dx_governance.intent_proposals.id"]),
        sa.ForeignKeyConstraint(["task_id"], ["dx_core.tasks.id"]),
        sa.UniqueConstraint(
            "proposal_id",
            "actor_type",
            "actor_id",
            "idempotency_key",
            name="uq_execution_report_idempotency",
        ),
        sa.CheckConstraint(
            "status IN ('started', 'succeeded', 'failed', 'cancelled')",
            name="ck_execution_report_status",
        ),
        schema="dx_governance",
    )
    op.create_index(
        "ix_report_proposal",
        "execution_reports",
        ["proposal_id", "created_at"],
        schema="dx_governance",
    )

    op.execute("GRANT USAGE ON SCHEMA dx_governance TO dx_api, dx_worker")
    op.execute("GRANT SELECT, INSERT, UPDATE ON dx_governance.intent_proposals TO dx_api")
    op.execute(
        "GRANT SELECT, INSERT ON dx_governance.review_decisions, "
        "dx_governance.approval_grants, dx_governance.execution_reports TO dx_api"
    )
    op.execute(
        "GRANT SELECT ON dx_governance.intent_proposals, dx_governance.approval_grants TO dx_worker"
    )
    op.execute("GRANT INSERT ON dx_governance.execution_reports TO dx_worker")


def downgrade() -> None:
    for table, index in (
        ("execution_reports", "ix_report_proposal"),
        ("approval_grants", "ix_approval_proposal"),
        ("review_decisions", "ix_review_proposal"),
        ("intent_proposals", "ix_proposal_task"),
    ):
        op.drop_index(index, table_name=table, schema="dx_governance")
        op.drop_table(table, schema="dx_governance")
    op.execute(sa.text("DROP SCHEMA dx_governance"))
