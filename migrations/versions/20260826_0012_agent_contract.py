"""Add the contract-only Agent registry and lifecycle ledger."""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "20260826_0012"
down_revision: str | None = "20260826_0011"
branch_labels: Sequence[str] | None = None
depends_on: Sequence[str] | None = None


def upgrade() -> None:
    op.execute(sa.text("CREATE SCHEMA dx_agent"))
    op.create_table(
        "agents",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("task_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("name", sa.String(128), nullable=False),
        sa.Column("version", sa.String(32), nullable=False),
        sa.Column("role", sa.String(64), nullable=False),
        sa.Column("contract", postgresql.JSONB(), nullable=False),
        sa.Column("allowed_capabilities", postgresql.JSONB(), nullable=False),
        sa.Column("status", sa.String(16), nullable=False, server_default="registered"),
        sa.Column("actor_type", sa.String(32), nullable=False),
        sa.Column("actor_id", sa.String(128), nullable=False),
        sa.Column("idempotency_key", sa.String(128), nullable=False),
        sa.Column("request_hash", sa.String(64), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.UniqueConstraint("name", "version", name="uq_agent_name_version"),
        sa.ForeignKeyConstraint(["task_id"], ["dx_core.tasks.id"]),
        sa.UniqueConstraint(
            "actor_type", "actor_id", "idempotency_key", name="uq_agent_idempotency"
        ),
        sa.CheckConstraint(
            "status IN ('registered', 'active', 'paused', 'retired')",
            name="ck_agent_status",
        ),
        schema="dx_agent",
    )
    op.create_index(
        "ix_agents_status_created", "agents", ["status", "created_at"], schema="dx_agent"
    )
    op.create_table(
        "agent_lifecycle_commands",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("agent_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("previous_status", sa.String(16), nullable=False),
        sa.Column("target_status", sa.String(16), nullable=False),
        sa.Column("reason", sa.Text(), nullable=False),
        sa.Column("actor_type", sa.String(32), nullable=False),
        sa.Column("actor_id", sa.String(128), nullable=False),
        sa.Column("idempotency_key", sa.String(128), nullable=False),
        sa.Column("request_hash", sa.String(64), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.ForeignKeyConstraint(["agent_id"], ["dx_agent.agents.id"]),
        sa.UniqueConstraint(
            "agent_id", "actor_type", "actor_id", "idempotency_key",
            name="uq_agent_lifecycle_idempotency",
        ),
        sa.CheckConstraint(
            "target_status IN ('active', 'paused', 'retired')",
            name="ck_agent_lifecycle_target_status",
        ),
        schema="dx_agent",
    )
    op.create_index(
        "ix_agent_lifecycle_agent_created",
        "agent_lifecycle_commands",
        ["agent_id", "created_at"],
        schema="dx_agent",
    )
    op.execute(sa.text("GRANT USAGE ON SCHEMA dx_agent TO dx_api, dx_worker"))
    op.execute(
        sa.text(
            "GRANT SELECT, INSERT, UPDATE ON dx_agent.agents, "
            "dx_agent.agent_lifecycle_commands TO dx_api"
        )
    )
    op.execute(
        sa.text(
            "GRANT SELECT ON dx_agent.agents, dx_agent.agent_lifecycle_commands TO dx_worker"
        )
    )


def downgrade() -> None:
    op.execute(
        sa.text(
            "REVOKE SELECT ON dx_agent.agents, dx_agent.agent_lifecycle_commands FROM dx_worker"
        )
    )
    op.execute(
        sa.text(
            "REVOKE SELECT, INSERT, UPDATE ON dx_agent.agents, "
            "dx_agent.agent_lifecycle_commands FROM dx_api"
        )
    )
    op.drop_index(
        "ix_agent_lifecycle_agent_created",
        table_name="agent_lifecycle_commands",
        schema="dx_agent",
    )
    op.drop_table("agent_lifecycle_commands", schema="dx_agent")
    op.drop_index("ix_agents_status_created", table_name="agents", schema="dx_agent")
    op.drop_table("agents", schema="dx_agent")
    op.execute(sa.text("DROP SCHEMA dx_agent"))
