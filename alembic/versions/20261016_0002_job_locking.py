"""job locking: locked_by token and one active job per version and step

Revision ID: 0002
Revises: 0001
Create Date: 2026-10-16
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0002"
down_revision: str | None = "0001"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("jobs", sa.Column("locked_by", sa.String(128), nullable=True))
    op.create_index(
        "uq_jobs_active_version_step",
        "jobs",
        ["version_id", "step"],
        unique=True,
        postgresql_where=sa.text("status IN ('queued', 'running')"),
    )


def downgrade() -> None:
    op.drop_index("uq_jobs_active_version_step", table_name="jobs")
    op.drop_column("jobs", "locked_by")
