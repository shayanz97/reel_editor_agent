"""bot bookkeeping: status/preview messages, notification timestamps, version approval

Revision ID: 0003
Revises: 0002
Create Date: 2026-10-17
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0003"
down_revision: str | None = "0002"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

TS = sa.DateTime(timezone=True)


def upgrade() -> None:
    op.add_column("projects", sa.Column("status_message_id", sa.BigInteger(), nullable=True))
    op.add_column("projects", sa.Column("notified_status", sa.String(32), nullable=True))
    op.add_column("versions", sa.Column("approved_at", TS, nullable=True))
    op.add_column("versions", sa.Column("preview_message_id", sa.BigInteger(), nullable=True))
    op.add_column("versions", sa.Column("preview_sent_at", TS, nullable=True))
    op.add_column("versions", sa.Column("failure_notified_at", TS, nullable=True))
    op.add_column("jobs", sa.Column("admin_notified_at", TS, nullable=True))


def downgrade() -> None:
    op.drop_column("jobs", "admin_notified_at")
    op.drop_column("versions", "failure_notified_at")
    op.drop_column("versions", "preview_sent_at")
    op.drop_column("versions", "preview_message_id")
    op.drop_column("versions", "approved_at")
    op.drop_column("projects", "notified_status")
    op.drop_column("projects", "status_message_id")
