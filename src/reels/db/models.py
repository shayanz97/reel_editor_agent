"""SQLAlchemy models. Every schema change needs an Alembic migration."""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import Any

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    DateTime,
    Dialect,
    ForeignKey,
    Index,
    MetaData,
    String,
    Text,
    TypeDecorator,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import ARRAY, JSONB
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship

from reels.settings_schema import ClientSettings

NAMING_CONVENTION = {
    "ix": "ix_%(column_0_N_label)s",
    "uq": "uq_%(table_name)s_%(column_0_N_name)s",
    "ck": "ck_%(table_name)s_%(constraint_name)s",
    "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
    "pk": "pk_%(table_name)s",
}


class Base(DeclarativeBase):
    metadata = MetaData(naming_convention=NAMING_CONVENTION)


# --- status enums (stored as VARCHAR + CHECK, easier to migrate than PG enums) ---


class ProjectStatus(StrEnum):
    RECEIVED = "received"
    PROCESSING = "processing"
    REVIEW = "review"
    APPROVED = "approved"
    FAILED = "failed"


class VersionStatus(StrEnum):
    PENDING = "pending"
    READY = "ready"
    FAILED = "failed"


class CorrectionStatus(StrEnum):
    RECEIVED = "received"
    PLANNED = "planned"
    NEEDS_CLARIFICATION = "needs_clarification"
    CONFIRMED = "confirmed"
    REJECTED = "rejected"
    APPLIED = "applied"


class GlossaryStatus(StrEnum):
    CONFIRMED = "confirmed"
    SUGGESTED = "suggested"


class JobStatus(StrEnum):
    QUEUED = "queued"
    RUNNING = "running"
    DONE = "done"
    FAILED = "failed"


class AssetKind(StrEnum):
    ORIGINAL = "original"
    AUDIO = "audio"
    PREVIEW = "preview"
    EXPORT = "export"
    COVER = "cover"
    OTHER = "other"


class StrEnumType[E: StrEnum](TypeDecorator[E]):
    """VARCHAR column that round-trips a StrEnum."""

    impl = String(32)
    cache_ok = True

    def __init__(self, enum_cls: type[E]) -> None:
        super().__init__()
        self.enum_cls = enum_cls

    def process_bind_param(self, value: E | str | None, dialect: Dialect) -> str | None:
        return None if value is None else self.enum_cls(value).value

    def process_result_value(self, value: str | None, dialect: Dialect) -> E | None:
        return None if value is None else self.enum_cls(value)


def _status_check(enum_cls: type[StrEnum], column: str = "status") -> CheckConstraint:
    allowed = ", ".join(f"'{m.value}'" for m in enum_cls)
    return CheckConstraint(f"{column} IN ({allowed})", name=f"{column}_valid")


def _created_at() -> Mapped[datetime]:
    return mapped_column(DateTime(timezone=True), server_default=func.now())


def _updated_at() -> Mapped[datetime]:
    return mapped_column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())


_EMPTY_JSON_OBJECT = text("'{}'::jsonb")
_EMPTY_JSON_ARRAY = text("'[]'::jsonb")


# --- tables ---


class Client(Base):
    __tablename__ = "clients"

    id: Mapped[int] = mapped_column(primary_key=True)
    slug: Mapped[str] = mapped_column(String(64), unique=True)
    name: Mapped[str] = mapped_column(String(200))
    settings: Mapped[dict[str, Any]] = mapped_column(
        JSONB, default=dict, server_default=_EMPTY_JSON_OBJECT
    )
    active: Mapped[bool] = mapped_column(default=True, server_default=text("true"))
    created_at: Mapped[datetime] = _created_at()
    updated_at: Mapped[datetime] = _updated_at()

    users: Mapped[list[ClientUser]] = relationship(
        back_populates="client", cascade="all, delete-orphan", order_by="ClientUser.id"
    )

    def client_settings(self) -> ClientSettings:
        return ClientSettings.model_validate(self.settings)


class ClientUser(Base):
    """A Telegram user allowed to talk to the bot on behalf of a client."""

    __tablename__ = "client_users"

    id: Mapped[int] = mapped_column(primary_key=True)
    client_id: Mapped[int] = mapped_column(ForeignKey("clients.id", ondelete="CASCADE"), index=True)
    telegram_user_id: Mapped[int] = mapped_column(BigInteger, unique=True)
    display_name: Mapped[str | None] = mapped_column(String(200))
    created_at: Mapped[datetime] = _created_at()

    client: Mapped[Client] = relationship(back_populates="users")


class Project(Base):
    """One incoming video and everything derived from it."""

    __tablename__ = "projects"
    __table_args__ = (
        _status_check(ProjectStatus),
        # Duplicate Telegram updates are detected via (chat, message).
        UniqueConstraint("telegram_chat_id", "telegram_message_id"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    client_id: Mapped[int] = mapped_column(ForeignKey("clients.id", ondelete="CASCADE"), index=True)
    status: Mapped[ProjectStatus] = mapped_column(
        StrEnumType(ProjectStatus), default=ProjectStatus.RECEIVED
    )
    title: Mapped[str | None] = mapped_column(String(200))
    # Optional text the client sent along with the video.
    caption: Mapped[str | None] = mapped_column(Text)
    telegram_chat_id: Mapped[int | None] = mapped_column(BigInteger)
    telegram_message_id: Mapped[int | None] = mapped_column(BigInteger)
    approved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = _created_at()
    updated_at: Mapped[datetime] = _updated_at()


class Asset(Base):
    """A file belonging to a project. `path` is relative to DATA_DIR."""

    __tablename__ = "assets"
    __table_args__ = (_status_check(AssetKind, column="kind"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    project_id: Mapped[int] = mapped_column(
        ForeignKey("projects.id", ondelete="CASCADE"), index=True
    )
    version_id: Mapped[int | None] = mapped_column(
        ForeignKey("versions.id", ondelete="SET NULL"), index=True
    )
    kind: Mapped[AssetKind] = mapped_column(StrEnumType(AssetKind))
    path: Mapped[str] = mapped_column(Text)
    size_bytes: Mapped[int | None] = mapped_column(BigInteger)
    sha256: Mapped[str | None] = mapped_column(String(64))
    meta: Mapped[dict[str, Any]] = mapped_column(
        JSONB, default=dict, server_default=_EMPTY_JSON_OBJECT
    )
    created_at: Mapped[datetime] = _created_at()


class Transcript(Base):
    """Word-level transcript on the original timeline. Never modified after creation."""

    __tablename__ = "transcripts"

    id: Mapped[int] = mapped_column(primary_key=True)
    project_id: Mapped[int] = mapped_column(
        ForeignKey("projects.id", ondelete="CASCADE"), index=True
    )
    engine: Mapped[str] = mapped_column(String(32))
    model: Mapped[str | None] = mapped_column(String(128))
    language: Mapped[str] = mapped_column(String(8), default="de", server_default="de")
    words: Mapped[list[Any]] = mapped_column(JSONB)
    params: Mapped[dict[str, Any]] = mapped_column(
        JSONB, default=dict, server_default=_EMPTY_JSON_OBJECT
    )
    created_at: Mapped[datetime] = _created_at()


class Version(Base):
    """One rendered state of a project. Always rendered from the original."""

    __tablename__ = "versions"
    __table_args__ = (
        _status_check(VersionStatus),
        UniqueConstraint("project_id", "number"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    project_id: Mapped[int] = mapped_column(
        ForeignKey("projects.id", ondelete="CASCADE"), index=True
    )
    number: Mapped[int]
    parent_id: Mapped[int | None] = mapped_column(ForeignKey("versions.id", ondelete="SET NULL"))
    transcript_id: Mapped[int | None] = mapped_column(
        ForeignKey("transcripts.id", ondelete="SET NULL")
    )
    status: Mapped[VersionStatus] = mapped_column(
        StrEnumType(VersionStatus), default=VersionStatus.PENDING
    )
    cut_plan: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    # Text corrections keyed by word id; the transcript itself stays untouched.
    text_overlay: Mapped[dict[str, Any]] = mapped_column(
        JSONB, default=dict, server_default=_EMPTY_JSON_OBJECT
    )
    highlights: Mapped[list[Any]] = mapped_column(
        JSONB, default=list, server_default=_EMPTY_JSON_ARRAY
    )
    subtitle_style: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    preview_path: Mapped[str | None] = mapped_column(Text)
    export_path: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = _created_at()
    updated_at: Mapped[datetime] = _updated_at()


class Correction(Base):
    """A free-text correction request against `version_id`."""

    __tablename__ = "corrections"
    __table_args__ = (_status_check(CorrectionStatus),)

    id: Mapped[int] = mapped_column(primary_key=True)
    version_id: Mapped[int] = mapped_column(
        ForeignKey("versions.id", ondelete="CASCADE"), index=True
    )
    result_version_id: Mapped[int | None] = mapped_column(
        ForeignKey("versions.id", ondelete="SET NULL")
    )
    message_text: Mapped[str] = mapped_column(Text)
    telegram_message_id: Mapped[int | None] = mapped_column(BigInteger)
    plan: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    status: Mapped[CorrectionStatus] = mapped_column(
        StrEnumType(CorrectionStatus), default=CorrectionStatus.RECEIVED
    )
    created_at: Mapped[datetime] = _created_at()
    updated_at: Mapped[datetime] = _updated_at()


class GlossaryEntry(Base):
    """Per-client technical vocabulary. `suggested` entries take effect only once confirmed."""

    __tablename__ = "glossary"
    __table_args__ = (
        _status_check(GlossaryStatus),
        UniqueConstraint("client_id", "term"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    client_id: Mapped[int] = mapped_column(ForeignKey("clients.id", ondelete="CASCADE"), index=True)
    term: Mapped[str] = mapped_column(String(200))
    variants: Mapped[list[str]] = mapped_column(
        ARRAY(Text), default=list, server_default=text("'{}'::text[]")
    )
    category: Mapped[str | None] = mapped_column(String(64))
    note: Mapped[str | None] = mapped_column(Text)
    highlight: Mapped[bool] = mapped_column(default=False, server_default=text("false"))
    source: Mapped[str] = mapped_column(String(32), default="manual", server_default="manual")
    status: Mapped[GlossaryStatus] = mapped_column(
        StrEnumType(GlossaryStatus), default=GlossaryStatus.CONFIRMED
    )
    hits: Mapped[int] = mapped_column(default=0, server_default=text("0"))
    created_at: Mapped[datetime] = _created_at()
    updated_at: Mapped[datetime] = _updated_at()


class Job(Base):
    """Queue entry; fetched with SELECT ... FOR UPDATE SKIP LOCKED."""

    __tablename__ = "jobs"
    __table_args__ = (
        _status_check(JobStatus),
        Index("ix_jobs_status_created_at", "status", "created_at"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    version_id: Mapped[int | None] = mapped_column(
        ForeignKey("versions.id", ondelete="CASCADE"), index=True
    )
    step: Mapped[str] = mapped_column(String(64))
    status: Mapped[JobStatus] = mapped_column(StrEnumType(JobStatus), default=JobStatus.QUEUED)
    attempts: Mapped[int] = mapped_column(default=0, server_default=text("0"))
    error: Mapped[str | None] = mapped_column(Text)
    locked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    metrics: Mapped[dict[str, Any]] = mapped_column(
        JSONB, default=dict, server_default=_EMPTY_JSON_OBJECT
    )
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = _created_at()
    updated_at: Mapped[datetime] = _updated_at()
