"""Grant the API role the update needed to accept a workflow handoff."""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "20260826_0011"
down_revision: str | None = "20260826_0010"
branch_labels: Sequence[str] | None = None
depends_on: Sequence[str] | None = None


def upgrade() -> None:
    op.execute(sa.text("GRANT UPDATE ON dx_governance.workflow_handoffs TO dx_api"))


def downgrade() -> None:
    op.execute(sa.text("REVOKE UPDATE ON dx_governance.workflow_handoffs FROM dx_api"))
