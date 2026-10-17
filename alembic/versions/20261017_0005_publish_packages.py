"""publish packages (covers, post text, hashtags)

Revision ID: 0005
Revises: 0004
Create Date: 2026-10-17
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0005"
down_revision: str | None = "0004"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

TS = sa.DateTime(timezone=True)


def upgrade() -> None:
    op.create_table(
        "publish_packages",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("version_id", sa.Integer(), nullable=False),
        sa.Column("status", sa.String(32), nullable=False),
        sa.Column(
            "candidates", postgresql.JSONB(), server_default=sa.text("'[]'::jsonb"), nullable=False
        ),
        sa.Column(
            "covers", postgresql.JSONB(), server_default=sa.text("'[]'::jsonb"), nullable=False
        ),
        sa.Column("selected_cover", sa.Integer(), nullable=True),
        sa.Column(
            "post", postgresql.JSONB(), server_default=sa.text("'{}'::jsonb"), nullable=False
        ),
        sa.Column("post_text", sa.Text(), nullable=True),
        sa.Column("text_confirmed_at", TS, nullable=True),
        sa.Column("revision_request", sa.Text(), nullable=True),
        sa.Column("revision", sa.Integer(), server_default=sa.text("0"), nullable=False),
        sa.Column("offer_sent_at", TS, nullable=True),
        sa.Column("text_message_id", sa.BigInteger(), nullable=True),
        sa.Column("text_sent_revision", sa.Integer(), server_default=sa.text("0"), nullable=False),
        sa.Column("final_sent_at", TS, nullable=True),
        sa.Column("failure_notified_at", TS, nullable=True),
        sa.Column("created_at", TS, server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", TS, server_default=sa.func.now(), nullable=False),
        sa.CheckConstraint(
            "status IN ('pending', 'ready', 'done', 'failed')",
            name=op.f("ck_publish_packages_status_valid"),
        ),
        sa.ForeignKeyConstraint(
            ["version_id"],
            ["versions.id"],
            name=op.f("fk_publish_packages_version_id_versions"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_publish_packages")),
        sa.UniqueConstraint("version_id", name=op.f("uq_publish_packages_version_id")),
    )


def downgrade() -> None:
    op.drop_table("publish_packages")
