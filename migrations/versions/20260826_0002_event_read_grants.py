"""Grant API read privilege needed by PostgreSQL INSERT ... RETURNING.

Revision ID: 20260826_0002
Revises: 20260825_0001
Create Date: 2026-08-26
"""

from collections.abc import Sequence

from alembic import op

revision: str = "20260826_0002"
down_revision: str | None = "20260825_0001"
branch_labels: Sequence[str] | None = None
depends_on: Sequence[str] | None = None


def upgrade() -> None:
    # SQLAlchemy obtains PostgreSQL server-generated timestamps through RETURNING.
    op.execute("GRANT SELECT ON dx_events.domain_events, dx_events.event_outbox TO dx_api")


def downgrade() -> None:
    op.execute("REVOKE SELECT ON dx_events.domain_events, dx_events.event_outbox FROM dx_api")
