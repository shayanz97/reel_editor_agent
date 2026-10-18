"""b-roll assets (with pgvector embedding) and placements

Revision ID: 0006
Revises: 0005
Create Date: 2026-10-18
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from pgvector.sqlalchemy import Vector
from sqlalchemy.dialects import postgresql

revision: str = "0006"
down_revision: str | None = "0005"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

TS = sa.DateTime(timezone=True)


def upgrade() -> None:
    op.create_table(
        "broll_assets",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("client_id", sa.Integer(), nullable=True),
        sa.Column("source", sa.String(32), nullable=False),
        sa.Column("source_id", sa.String(64), nullable=True),
        sa.Column("source_url", sa.Text(), nullable=True),
        sa.Column("media_type", sa.String(16), nullable=False),
        sa.Column("path", sa.Text(), nullable=True),
        sa.Column("thumbnail", sa.Text(), nullable=True),
        sa.Column("width", sa.Integer(), nullable=True),
        sa.Column("height", sa.Integer(), nullable=True),
        sa.Column("duration_s", sa.Float(), nullable=True),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("embedding", Vector(1536), nullable=True),
        sa.Column("status", sa.String(32), nullable=False),
        sa.Column(
            "meta", postgresql.JSONB(), server_default=sa.text("'{}'::jsonb"), nullable=False
        ),
        sa.Column("created_at", TS, server_default=sa.func.now(), nullable=False),
        sa.CheckConstraint(
            "source IN ('library', 'pixabay', 'textcard')",
            name=op.f("ck_broll_assets_source_valid"),
        ),
        sa.CheckConstraint(
            "status IN ('pending', 'ready', 'failed')", name=op.f("ck_broll_assets_status_valid")
        ),
        sa.ForeignKeyConstraint(
            ["client_id"],
            ["clients.id"],
            name=op.f("fk_broll_assets_client_id_clients"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_broll_assets")),
        sa.UniqueConstraint("source", "source_id", name=op.f("uq_broll_assets_source_source_id")),
    )
    op.create_index(op.f("ix_broll_assets_client_id"), "broll_assets", ["client_id"])

    op.create_table(
        "broll_placements",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("version_id", sa.Integer(), nullable=False),
        sa.Column("number", sa.Integer(), nullable=False),
        sa.Column("first_word", sa.Integer(), nullable=False),
        sa.Column("last_word", sa.Integer(), nullable=False),
        sa.Column("kind", sa.String(16), nullable=False),
        sa.Column("idea", sa.Text(), nullable=False),
        sa.Column("query", sa.Text(), nullable=True),
        sa.Column("card_text", sa.Text(), nullable=True),
        sa.Column("priority", sa.Integer(), server_default=sa.text("2"), nullable=False),
        sa.Column("asset_id", sa.Integer(), nullable=True),
        sa.Column("score", sa.Float(), nullable=True),
        sa.Column("status", sa.String(32), nullable=False),
        sa.Column("reason", sa.Text(), nullable=True),
        sa.Column("created_at", TS, server_default=sa.func.now(), nullable=False),
        sa.CheckConstraint(
            "status IN ('planned', 'ready', 'skipped')",
            name=op.f("ck_broll_placements_status_valid"),
        ),
        sa.ForeignKeyConstraint(
            ["version_id"],
            ["versions.id"],
            name=op.f("fk_broll_placements_version_id_versions"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["asset_id"],
            ["broll_assets.id"],
            name=op.f("fk_broll_placements_asset_id_broll_assets"),
            ondelete="SET NULL",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_broll_placements")),
        sa.UniqueConstraint(
            "version_id", "number", name=op.f("uq_broll_placements_version_id_number")
        ),
    )
    op.create_index(op.f("ix_broll_placements_version_id"), "broll_placements", ["version_id"])


def downgrade() -> None:
    op.drop_table("broll_placements")
    op.drop_table("broll_assets")
