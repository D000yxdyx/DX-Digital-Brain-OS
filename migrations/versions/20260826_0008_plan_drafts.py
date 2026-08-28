"""Add deterministic model-plan drafts awaiting human confirmation."""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "20260826_0008"
down_revision: str | None = "20260826_0007"
branch_labels: Sequence[str] | None = None
depends_on: Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "plan_drafts",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "task_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("dx_core.tasks.id"),
            nullable=False,
        ),
        sa.Column(
            "proposal_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("dx_governance.intent_proposals.id"),
            nullable=False,
        ),
        sa.Column(
            "model_call_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("dx_model.model_calls.id"),
            nullable=False,
        ),
        sa.Column("status", sa.String(16), nullable=False, server_default="candidate"),
        sa.Column("parser_version", sa.String(32), nullable=False),
        sa.Column("source_hash", sa.String(64), nullable=False),
        sa.Column("steps", postgresql.JSONB, nullable=False),
        sa.Column("warnings", postgresql.JSONB, nullable=False),
        sa.Column("actor_type", sa.String(32), nullable=False),
        sa.Column("actor_id", sa.String(128), nullable=False),
        sa.Column("idempotency_key", sa.String(128), nullable=False),
        sa.Column("request_hash", sa.String(64), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.Column("reviewed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("reviewer_actor_type", sa.String(32), nullable=True),
        sa.Column("reviewer_actor_id", sa.String(128), nullable=True),
        sa.Column("review_reason", sa.Text, nullable=True),
        sa.Column("review_idempotency_key", sa.String(128), nullable=True),
        sa.Column("review_request_hash", sa.String(64), nullable=True),
        sa.CheckConstraint(
            "status IN ('candidate', 'confirmed', 'rejected')",
            name="ck_plan_draft_status",
        ),
        sa.UniqueConstraint(
            "actor_type",
            "actor_id",
            "idempotency_key",
            name="uq_plan_draft_idempotency",
        ),
        schema="dx_governance",
    )
    op.create_index(
        "ix_plan_drafts_model_call_created",
        "plan_drafts",
        ["model_call_id", "created_at"],
        schema="dx_governance",
    )
    op.create_index(
        "ix_plan_drafts_proposal_created",
        "plan_drafts",
        ["proposal_id", "created_at"],
        schema="dx_governance",
    )
    op.execute(sa.text("GRANT SELECT, INSERT, UPDATE ON dx_governance.plan_drafts TO dx_api"))
    op.execute(sa.text("GRANT SELECT ON dx_governance.plan_drafts TO dx_worker"))


def downgrade() -> None:
    op.execute(sa.text("REVOKE SELECT, INSERT, UPDATE ON dx_governance.plan_drafts FROM dx_api"))
    op.execute(sa.text("REVOKE SELECT ON dx_governance.plan_drafts FROM dx_worker"))
    op.drop_index(
        "ix_plan_drafts_proposal_created", table_name="plan_drafts", schema="dx_governance"
    )
    op.drop_index(
        "ix_plan_drafts_model_call_created", table_name="plan_drafts", schema="dx_governance"
    )
    op.drop_table("plan_drafts", schema="dx_governance")
