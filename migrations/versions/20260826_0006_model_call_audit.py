"""Add structured model-call audit records for dry-run and future live calls."""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "20260826_0006"
down_revision: str | None = "20260826_0005"
branch_labels: Sequence[str] | None = None
depends_on: Sequence[str] | None = None


def upgrade() -> None:
    op.execute(sa.text("CREATE SCHEMA dx_model"))
    op.create_table(
        "model_calls",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("task_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("proposal_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("role", sa.String(32), nullable=False),
        sa.Column("mode", sa.String(16), nullable=False),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("provider", sa.String(32), nullable=False),
        sa.Column("model", sa.String(128), nullable=False),
        sa.Column("request_hash", sa.String(64), nullable=False),
        sa.Column("request_summary", postgresql.JSONB(), nullable=False),
        sa.Column("response_summary", postgresql.JSONB(), nullable=False),
        sa.Column("input_tokens", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("output_tokens", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("total_tokens", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("cost_usd", sa.Float(), nullable=True),
        sa.Column("latency_ms", sa.Integer(), nullable=True),
        sa.Column("error_code", sa.String(64), nullable=True),
        sa.Column("actor_type", sa.String(32), nullable=False),
        sa.Column("actor_id", sa.String(128), nullable=False),
        sa.Column("idempotency_key", sa.String(128), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(["task_id"], ["dx_core.tasks.id"]),
        sa.ForeignKeyConstraint(["proposal_id"], ["dx_governance.intent_proposals.id"]),
        sa.UniqueConstraint(
            "actor_type", "actor_id", "idempotency_key", name="uq_model_call_idempotency"
        ),
        sa.CheckConstraint("mode IN ('dry_run', 'live')", name="ck_model_call_mode"),
        sa.CheckConstraint(
            "status IN ('dry_run', 'succeeded', 'failed', 'blocked')",
            name="ck_model_call_status",
        ),
        sa.CheckConstraint(
            "input_tokens >= 0 AND output_tokens >= 0 AND total_tokens >= 0",
            name="ck_model_call_tokens_nonnegative",
        ),
        schema="dx_model",
    )
    op.create_index(
        "ix_model_calls_task_created", "model_calls", ["task_id", "created_at"], schema="dx_model"
    )
    op.create_index(
        "ix_model_calls_proposal_created",
        "model_calls",
        ["proposal_id", "created_at"],
        schema="dx_model",
    )
    op.execute(sa.text("GRANT USAGE ON SCHEMA dx_model TO dx_api, dx_worker"))
    op.execute(sa.text("GRANT SELECT, INSERT ON dx_model.model_calls TO dx_api"))
    op.execute(sa.text("GRANT SELECT ON dx_model.model_calls TO dx_worker"))


def downgrade() -> None:
    op.drop_index("ix_model_calls_proposal_created", table_name="model_calls", schema="dx_model")
    op.drop_index("ix_model_calls_task_created", table_name="model_calls", schema="dx_model")
    op.drop_table("model_calls", schema="dx_model")
    op.execute(sa.text("DROP SCHEMA dx_model"))
