"""version edits from corrections, correction notification bookkeeping

Revision ID: 0004
Revises: 0003
Create Date: 2026-10-17
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0004"
down_revision: str | None = "0003"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "versions",
        sa.Column(
            "edits", postgresql.JSONB(), server_default=sa.text("'{}'::jsonb"), nullable=False
        ),
    )
    op.add_column("corrections", sa.Column("notified_at", sa.DateTime(timezone=True)))
    op.add_column("corrections", sa.Column("summary_message_id", sa.BigInteger()))


def downgrade() -> None:
    op.drop_column("corrections", "summary_message_id")
    op.drop_column("corrections", "notified_at")
    op.drop_column("versions", "edits")
