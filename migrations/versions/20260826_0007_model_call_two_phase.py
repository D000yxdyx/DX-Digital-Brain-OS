"""Add prepared/finalized lifecycle fields to the model-call ledger."""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "20260826_0007"
down_revision: str | None = "20260826_0006"
branch_labels: Sequence[str] | None = None
depends_on: Sequence[str] | None = None


def upgrade() -> None:
    op.drop_constraint("ck_model_call_status", "model_calls", schema="dx_model", type_="check")
    op.create_check_constraint(
        "ck_model_call_status",
        "model_calls",
        "status IN ('prepared', 'dry_run', 'succeeded', 'failed', 'blocked')",
        schema="dx_model",
    )
    op.add_column(
        "model_calls",
        sa.Column("finalize_idempotency_key", sa.String(128), nullable=True),
        schema="dx_model",
    )
    op.add_column(
        "model_calls",
        sa.Column("finalize_request_hash", sa.String(64), nullable=True),
        schema="dx_model",
    )
    op.add_column(
        "model_calls",
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        schema="dx_model",
    )
    op.add_column(
        "model_calls",
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        schema="dx_model",
    )
    op.execute(sa.text("GRANT UPDATE ON dx_model.model_calls TO dx_api"))


def downgrade() -> None:
    op.execute(sa.text("REVOKE UPDATE ON dx_model.model_calls FROM dx_api"))
    for column in (
        "finished_at",
        "started_at",
        "finalize_request_hash",
        "finalize_idempotency_key",
    ):
        op.drop_column("model_calls", column, schema="dx_model")
    op.drop_constraint("ck_model_call_status", "model_calls", schema="dx_model", type_="check")
    op.create_check_constraint(
        "ck_model_call_status",
        "model_calls",
        "status IN ('dry_run', 'succeeded', 'failed', 'blocked')",
        schema="dx_model",
    )
