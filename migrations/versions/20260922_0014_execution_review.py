"""Add the human ExecutionReview verdict for the Operator / Task Card MVP (E.4).

One table in the existing ``dx_governance`` schema, FK to
``dx_workflow.agent_executions.id``, plus the minimum dx_api grant. The verdict is
independent of ``agent_executions.status``: a succeeded execution may be rejected by
the human without mutating the execution's technical state.

dx_api owns ExecutionReview (read/write). dx_worker is not granted write access and
is not granted any access here at all (the worker never routes this event), matching
the requirement that dx_worker permissions not be broadened.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "20260922_0014"
down_revision: str | None = "20260921_0013"
branch_labels: Sequence[str] | None = None
depends_on: Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "execution_reviews",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("agent_execution_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("decision", sa.String(16), nullable=False),
        sa.Column("reason", sa.Text(), nullable=False),
        sa.Column("actor_type", sa.String(32), nullable=False),
        sa.Column("actor_id", sa.String(128), nullable=False),
        sa.Column("idempotency_key", sa.String(128), nullable=False),
        sa.Column("request_hash", sa.String(64), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.ForeignKeyConstraint(
            ["agent_execution_id"], ["dx_workflow.agent_executions.id"]
        ),
        # exactly one final review per execution for MVP
        sa.UniqueConstraint("agent_execution_id", name="uq_execution_review_execution"),
        sa.CheckConstraint(
            "decision IN ('accepted', 'rejected')", name="ck_execution_review_decision"
        ),
        schema="dx_governance",
    )
    op.create_index(
        "ix_execution_reviews_execution_created",
        "execution_reviews",
        ["agent_execution_id", "created_at"],
        schema="dx_governance",
    )

    # dx_governance USAGE already granted in migration 0004. The verdict is
    # operator/API-owned state: dx_api writes, dx_worker gets nothing.
    op.execute(
        sa.text(
            "GRANT SELECT, INSERT ON dx_governance.execution_reviews TO dx_api"
        )
    )


def downgrade() -> None:
    op.execute(
        sa.text("REVOKE SELECT, INSERT ON dx_governance.execution_reviews FROM dx_api")
    )
    op.drop_index(
        "ix_execution_reviews_execution_created",
        table_name="execution_reviews",
        schema="dx_governance",
    )
    op.drop_table("execution_reviews", schema="dx_governance")
