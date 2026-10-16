"""initial schema

Revision ID: 0001
Revises:
Create Date: 2026-10-16
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0001"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

EMPTY_OBJECT = sa.text("'{}'::jsonb")
EMPTY_ARRAY = sa.text("'[]'::jsonb")


def _ts(name: str) -> sa.Column[object]:
    return sa.Column(name, sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False)


def _status(table: str, column: str, values: Sequence[str]) -> sa.CheckConstraint:
    allowed = ", ".join(f"'{v}'" for v in values)
    return sa.CheckConstraint(f"{column} IN ({allowed})", name=op.f(f"ck_{table}_{column}_valid"))


def upgrade() -> None:
    # pgvector is needed from Etappe 6 (B-roll embeddings); enable it right away.
    op.execute("CREATE EXTENSION IF NOT EXISTS vector")

    op.create_table(
        "clients",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("slug", sa.String(64), nullable=False),
        sa.Column("name", sa.String(200), nullable=False),
        sa.Column("settings", postgresql.JSONB(), server_default=EMPTY_OBJECT, nullable=False),
        sa.Column("active", sa.Boolean(), server_default=sa.text("true"), nullable=False),
        _ts("created_at"),
        _ts("updated_at"),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_clients")),
        sa.UniqueConstraint("slug", name=op.f("uq_clients_slug")),
    )

    op.create_table(
        "client_users",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("client_id", sa.Integer(), nullable=False),
        sa.Column("telegram_user_id", sa.BigInteger(), nullable=False),
        sa.Column("display_name", sa.String(200), nullable=True),
        _ts("created_at"),
        sa.ForeignKeyConstraint(
            ["client_id"],
            ["clients.id"],
            name=op.f("fk_client_users_client_id_clients"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_client_users")),
        sa.UniqueConstraint("telegram_user_id", name=op.f("uq_client_users_telegram_user_id")),
    )
    op.create_index(op.f("ix_client_users_client_id"), "client_users", ["client_id"])

    op.create_table(
        "projects",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("client_id", sa.Integer(), nullable=False),
        sa.Column("status", sa.String(32), nullable=False),
        sa.Column("title", sa.String(200), nullable=True),
        sa.Column("caption", sa.Text(), nullable=True),
        sa.Column("telegram_chat_id", sa.BigInteger(), nullable=True),
        sa.Column("telegram_message_id", sa.BigInteger(), nullable=True),
        sa.Column("approved_at", sa.DateTime(timezone=True), nullable=True),
        _ts("created_at"),
        _ts("updated_at"),
        _status("projects", "status", ["received", "processing", "review", "approved", "failed"]),
        sa.ForeignKeyConstraint(
            ["client_id"],
            ["clients.id"],
            name=op.f("fk_projects_client_id_clients"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_projects")),
        sa.UniqueConstraint(
            "telegram_chat_id",
            "telegram_message_id",
            name=op.f("uq_projects_telegram_chat_id_telegram_message_id"),
        ),
    )
    op.create_index(op.f("ix_projects_client_id"), "projects", ["client_id"])

    op.create_table(
        "transcripts",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("project_id", sa.Integer(), nullable=False),
        sa.Column("engine", sa.String(32), nullable=False),
        sa.Column("model", sa.String(128), nullable=True),
        sa.Column("language", sa.String(8), server_default="de", nullable=False),
        sa.Column("words", postgresql.JSONB(), nullable=False),
        sa.Column("params", postgresql.JSONB(), server_default=EMPTY_OBJECT, nullable=False),
        _ts("created_at"),
        sa.ForeignKeyConstraint(
            ["project_id"],
            ["projects.id"],
            name=op.f("fk_transcripts_project_id_projects"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_transcripts")),
    )
    op.create_index(op.f("ix_transcripts_project_id"), "transcripts", ["project_id"])

    op.create_table(
        "versions",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("project_id", sa.Integer(), nullable=False),
        sa.Column("number", sa.Integer(), nullable=False),
        sa.Column("parent_id", sa.Integer(), nullable=True),
        sa.Column("transcript_id", sa.Integer(), nullable=True),
        sa.Column("status", sa.String(32), nullable=False),
        sa.Column("cut_plan", postgresql.JSONB(), nullable=True),
        sa.Column("text_overlay", postgresql.JSONB(), server_default=EMPTY_OBJECT, nullable=False),
        sa.Column("highlights", postgresql.JSONB(), server_default=EMPTY_ARRAY, nullable=False),
        sa.Column("subtitle_style", postgresql.JSONB(), nullable=True),
        sa.Column("preview_path", sa.Text(), nullable=True),
        sa.Column("export_path", sa.Text(), nullable=True),
        _ts("created_at"),
        _ts("updated_at"),
        _status("versions", "status", ["pending", "ready", "failed"]),
        sa.ForeignKeyConstraint(
            ["project_id"],
            ["projects.id"],
            name=op.f("fk_versions_project_id_projects"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["parent_id"],
            ["versions.id"],
            name=op.f("fk_versions_parent_id_versions"),
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["transcript_id"],
            ["transcripts.id"],
            name=op.f("fk_versions_transcript_id_transcripts"),
            ondelete="SET NULL",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_versions")),
        sa.UniqueConstraint("project_id", "number", name=op.f("uq_versions_project_id_number")),
    )
    op.create_index(op.f("ix_versions_project_id"), "versions", ["project_id"])

    op.create_table(
        "assets",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("project_id", sa.Integer(), nullable=False),
        sa.Column("version_id", sa.Integer(), nullable=True),
        sa.Column("kind", sa.String(32), nullable=False),
        sa.Column("path", sa.Text(), nullable=False),
        sa.Column("size_bytes", sa.BigInteger(), nullable=True),
        sa.Column("sha256", sa.String(64), nullable=True),
        sa.Column("meta", postgresql.JSONB(), server_default=EMPTY_OBJECT, nullable=False),
        _ts("created_at"),
        _status("assets", "kind", ["original", "audio", "preview", "export", "cover", "other"]),
        sa.ForeignKeyConstraint(
            ["project_id"],
            ["projects.id"],
            name=op.f("fk_assets_project_id_projects"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["version_id"],
            ["versions.id"],
            name=op.f("fk_assets_version_id_versions"),
            ondelete="SET NULL",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_assets")),
    )
    op.create_index(op.f("ix_assets_project_id"), "assets", ["project_id"])
    op.create_index(op.f("ix_assets_version_id"), "assets", ["version_id"])

    op.create_table(
        "corrections",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("version_id", sa.Integer(), nullable=False),
        sa.Column("result_version_id", sa.Integer(), nullable=True),
        sa.Column("message_text", sa.Text(), nullable=False),
        sa.Column("telegram_message_id", sa.BigInteger(), nullable=True),
        sa.Column("plan", postgresql.JSONB(), nullable=True),
        sa.Column("status", sa.String(32), nullable=False),
        _ts("created_at"),
        _ts("updated_at"),
        _status(
            "corrections",
            "status",
            [
                "received",
                "planned",
                "needs_clarification",
                "confirmed",
                "rejected",
                "applied",
            ],
        ),
        sa.ForeignKeyConstraint(
            ["version_id"],
            ["versions.id"],
            name=op.f("fk_corrections_version_id_versions"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["result_version_id"],
            ["versions.id"],
            name=op.f("fk_corrections_result_version_id_versions"),
            ondelete="SET NULL",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_corrections")),
    )
    op.create_index(op.f("ix_corrections_version_id"), "corrections", ["version_id"])

    op.create_table(
        "glossary",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("client_id", sa.Integer(), nullable=False),
        sa.Column("term", sa.String(200), nullable=False),
        sa.Column(
            "variants",
            postgresql.ARRAY(sa.Text()),
            server_default=sa.text("'{}'::text[]"),
            nullable=False,
        ),
        sa.Column("category", sa.String(64), nullable=True),
        sa.Column("note", sa.Text(), nullable=True),
        sa.Column("highlight", sa.Boolean(), server_default=sa.text("false"), nullable=False),
        sa.Column("source", sa.String(32), server_default="manual", nullable=False),
        sa.Column("status", sa.String(32), nullable=False),
        sa.Column("hits", sa.Integer(), server_default=sa.text("0"), nullable=False),
        _ts("created_at"),
        _ts("updated_at"),
        _status("glossary", "status", ["confirmed", "suggested"]),
        sa.ForeignKeyConstraint(
            ["client_id"],
            ["clients.id"],
            name=op.f("fk_glossary_client_id_clients"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_glossary")),
        sa.UniqueConstraint("client_id", "term", name=op.f("uq_glossary_client_id_term")),
    )
    op.create_index(op.f("ix_glossary_client_id"), "glossary", ["client_id"])

    op.create_table(
        "jobs",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("version_id", sa.Integer(), nullable=True),
        sa.Column("step", sa.String(64), nullable=False),
        sa.Column("status", sa.String(32), nullable=False),
        sa.Column("attempts", sa.Integer(), server_default=sa.text("0"), nullable=False),
        sa.Column("error", sa.Text(), nullable=True),
        sa.Column("locked_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("metrics", postgresql.JSONB(), server_default=EMPTY_OBJECT, nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        _ts("created_at"),
        _ts("updated_at"),
        _status("jobs", "status", ["queued", "running", "done", "failed"]),
        sa.ForeignKeyConstraint(
            ["version_id"],
            ["versions.id"],
            name=op.f("fk_jobs_version_id_versions"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_jobs")),
    )
    op.create_index(op.f("ix_jobs_version_id"), "jobs", ["version_id"])
    op.create_index("ix_jobs_status_created_at", "jobs", ["status", "created_at"])


def downgrade() -> None:
    for table in (
        "jobs",
        "glossary",
        "corrections",
        "assets",
        "versions",
        "transcripts",
        "projects",
        "client_users",
        "clients",
    ):
        op.drop_table(table)
    # The vector extension is left in place; other database objects may depend on it.
