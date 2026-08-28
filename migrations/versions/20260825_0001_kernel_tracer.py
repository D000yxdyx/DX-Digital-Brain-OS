"""Create the Kernel Tracer Bullet schemas and least-privilege grants.

Revision ID: 20260825_0001
Revises:
Create Date: 2026-08-25
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "20260825_0001"
down_revision: str | None = None
branch_labels: Sequence[str] | None = None
depends_on: Sequence[str] | None = None


def upgrade() -> None:
    for schema in ("dx_core", "dx_events", "dx_state", "dx_audit"):
        op.execute(sa.text(f"CREATE SCHEMA {schema}"))

    op.create_table(
        "tasks",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("title", sa.String(240), nullable=False),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint("length(title) > 0", name="ck_task_title_nonempty"),
        schema="dx_core",
    )
    op.create_table(
        "idempotency_requests",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("actor_type", sa.String(32), nullable=False),
        sa.Column("actor_id", sa.String(128), nullable=False),
        sa.Column("operation", sa.String(80), nullable=False),
        sa.Column("idempotency_key", sa.String(128), nullable=False),
        sa.Column("request_hash", sa.String(64), nullable=False),
        sa.Column("task_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(["task_id"], ["dx_core.tasks.id"]),
        sa.UniqueConstraint(
            "actor_type", "actor_id", "operation", "idempotency_key", name="uq_idempotency_scope"
        ),
        schema="dx_core",
    )
    op.create_table(
        "domain_events",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("aggregate_type", sa.String(64), nullable=False),
        sa.Column("aggregate_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("event_type", sa.String(128), nullable=False),
        sa.Column("event_version", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("payload", postgresql.JSONB(), nullable=False),
        sa.Column(
            "occurred_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(["aggregate_id"], ["dx_core.tasks.id"]),
        schema="dx_events",
    )
    op.create_table(
        "entity_states",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("entity_type", sa.String(64), nullable=False),
        sa.Column("entity_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("status", sa.String(64), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("last_event_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(["entity_id"], ["dx_core.tasks.id"]),
        sa.ForeignKeyConstraint(["last_event_id"], ["dx_events.domain_events.id"]),
        sa.UniqueConstraint("entity_type", "entity_id", name="uq_entity_current_state"),
        sa.CheckConstraint("version >= 1", name="ck_state_version_positive"),
        schema="dx_state",
    )
    op.create_table(
        "state_transitions",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("entity_type", sa.String(64), nullable=False),
        sa.Column("entity_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("previous_status", sa.String(64), nullable=True),
        sa.Column("next_status", sa.String(64), nullable=False),
        sa.Column("event_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column(
            "occurred_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(["entity_id"], ["dx_core.tasks.id"]),
        sa.ForeignKeyConstraint(["event_id"], ["dx_events.domain_events.id"]),
        sa.UniqueConstraint(
            "entity_type", "entity_id", "version", name="uq_state_transition_version"
        ),
        schema="dx_state",
    )
    op.create_table(
        "event_outbox",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("event_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("status", sa.String(16), nullable=False, server_default="pending"),
        sa.Column("lease_owner", sa.String(128), nullable=True),
        sa.Column("lease_until", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("published_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(["event_id"], ["dx_events.domain_events.id"]),
        sa.UniqueConstraint("event_id", name="uq_outbox_event"),
        sa.CheckConstraint(
            "status IN ('pending', 'processing', 'published')", name="ck_outbox_status"
        ),
        schema="dx_events",
    )
    op.create_index(
        "ix_outbox_dispatch",
        "event_outbox",
        ["status", "lease_until", "created_at"],
        schema="dx_events",
    )
    op.create_table(
        "event_inbox",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("consumer_name", sa.String(128), nullable=False),
        sa.Column("event_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column(
            "received_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(["event_id"], ["dx_events.domain_events.id"]),
        sa.UniqueConstraint("consumer_name", "event_id", name="uq_inbox_consumer_event"),
        schema="dx_events",
    )
    op.create_table(
        "audit_events",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("actor_type", sa.String(32), nullable=False),
        sa.Column("actor_id", sa.String(128), nullable=False),
        sa.Column("operation", sa.String(80), nullable=False),
        sa.Column("task_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("event_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("detail", postgresql.JSONB(), nullable=False),
        sa.Column(
            "occurred_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(["task_id"], ["dx_core.tasks.id"]),
        sa.ForeignKeyConstraint(["event_id"], ["dx_events.domain_events.id"]),
        schema="dx_audit",
    )

    op.execute("GRANT USAGE ON SCHEMA dx_core, dx_events, dx_state, dx_audit TO dx_api, dx_worker")
    op.execute("GRANT SELECT, INSERT ON dx_core.tasks, dx_core.idempotency_requests TO dx_api")
    op.execute("GRANT SELECT, INSERT, UPDATE ON dx_state.entity_states TO dx_api")
    op.execute("GRANT SELECT, INSERT ON dx_state.state_transitions TO dx_api")
    op.execute("GRANT INSERT ON dx_events.domain_events, dx_events.event_outbox TO dx_api")
    op.execute("GRANT SELECT, INSERT ON dx_audit.audit_events TO dx_api")
    op.execute("GRANT SELECT, UPDATE ON dx_events.event_outbox TO dx_worker")
    op.execute("GRANT INSERT ON dx_events.event_inbox TO dx_worker")


def downgrade() -> None:
    op.drop_table("audit_events", schema="dx_audit")
    op.drop_table("event_inbox", schema="dx_events")
    op.drop_index("ix_outbox_dispatch", table_name="event_outbox", schema="dx_events")
    op.drop_table("event_outbox", schema="dx_events")
    op.drop_table("state_transitions", schema="dx_state")
    op.drop_table("entity_states", schema="dx_state")
    op.drop_table("domain_events", schema="dx_events")
    op.drop_table("idempotency_requests", schema="dx_core")
    op.drop_table("tasks", schema="dx_core")
    for schema in ("dx_audit", "dx_state", "dx_events", "dx_core"):
        op.execute(sa.text(f"DROP SCHEMA {schema}"))
