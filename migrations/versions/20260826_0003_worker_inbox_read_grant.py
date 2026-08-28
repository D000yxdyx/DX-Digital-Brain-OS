"""Grant worker read privilege needed by PostgreSQL INSERT ... RETURNING.

Revision ID: 20260826_0003
Revises: 20260826_0002
Create Date: 2026-08-26
"""

from collections.abc import Sequence

from alembic import op

revision: str = "20260826_0003"
down_revision: str | None = "20260826_0002"
branch_labels: Sequence[str] | None = None
depends_on: Sequence[str] | None = None


def upgrade() -> None:
    op.execute("GRANT SELECT ON dx_events.event_inbox TO dx_worker")


def downgrade() -> None:
    op.execute("REVOKE SELECT ON dx_events.event_inbox FROM dx_worker")
