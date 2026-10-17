"""Database side of the bot (synchronous, no Telegram types; called via asyncio.to_thread).

The bot never processes media. It registers incoming videos as projects (the worker takes
over) and reads back what changed, so it can tell the client.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from pathlib import Path
from typing import Any

from pydantic import TypeAdapter
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from reels.config import Settings
from reels.db import queue
from reels.db.clients import ClientError, create_client, get_client
from reels.db.models import (
    Client,
    ClientUser,
    Correction,
    CorrectionStatus,
    Job,
    JobStatus,
    Project,
    ProjectStatus,
    Version,
    VersionStatus,
)
from reels.db.models import Transcript as TranscriptRow
from reels.domain.corrections import (
    AnyOperation,
    CorrectionPlan,
    Issue,
    Note,
    Resolved,
)
from reels.domain.corrections import apply as apply_ops
from reels.domain.segments import Segment
from reels.domain.words import Word
from reels.pipeline.runner import (
    CORRECTION_STEP,
    create_next_version,
    create_project,
    version_state,
)
from reels.pipeline.steps import ProjectPaths

_SEGMENTS = TypeAdapter(list[Segment])
_TEXTS = TypeAdapter(list[str])


@dataclass(frozen=True)
class Access:
    client_id: int
    client_slug: str
    client_name: str


def find_access(session: Session, telegram_user_id: int) -> Access | None:
    row = session.execute(
        select(Client.id, Client.slug, Client.name)
        .join(ClientUser, ClientUser.client_id == Client.id)
        .where(ClientUser.telegram_user_id == telegram_user_id, Client.active.is_(True))
    ).first()
    return Access(*row) if row else None


# --- incoming videos ----------------------------------------------------------


class DuplicateMessageError(Exception):
    """The Telegram message was already registered (update delivered twice)."""


def is_known_message(session: Session, chat_id: int, message_id: int) -> bool:
    return (
        session.scalar(
            select(Project.id).where(
                Project.telegram_chat_id == chat_id, Project.telegram_message_id == message_id
            )
        )
        is not None
    )


def register_video(
    session: Session,
    access: Access,
    source: Path,
    app: Settings,
    *,
    chat_id: int,
    message_id: int,
    status_message_id: int | None,
    caption: str | None,
    title: str | None,
) -> tuple[int, int]:
    """Create project + version 1 and queue the pipeline. Returns (project_id, version_id)."""
    client = session.get(Client, access.client_id)
    assert client is not None
    try:
        with session.begin_nested():
            version = create_project(
                session,
                client,
                source,
                app,
                title=title,
                caption=caption,
                telegram_chat_id=chat_id,
                telegram_message_id=message_id,
            )
    except IntegrityError as exc:
        raise DuplicateMessageError(f"message {chat_id}/{message_id} already registered") from exc
    project = session.get(Project, version.project_id)
    assert project is not None
    project.status_message_id = status_message_id
    project.notified_status = ProjectStatus.RECEIVED.value
    return project.id, version.id


# --- notifications --------------------------------------------------------------


@dataclass(frozen=True)
class ProcessingNotice:
    project_id: int
    chat_id: int
    status_message_id: int | None


@dataclass(frozen=True)
class PreviewNotice:
    project_id: int
    version_id: int
    version_number: int
    chat_id: int
    status_message_id: int | None
    export: Path
    segments: list[Segment]
    duration_s: float | None
    removed: list[str] = field(default_factory=list)  # texts of segments removed by corrections


@dataclass(frozen=True)
class FailureNotice:
    project_id: int
    version_id: int
    chat_id: int
    status_message_id: int | None
    error_code: str | None
    error_details: dict[str, Any]


@dataclass(frozen=True)
class AdminJobNotice:
    job_id: int
    step: str
    attempts: int
    error: str
    client_name: str | None
    project_id: int | None
    version_number: int | None


@dataclass(frozen=True)
class CorrectionPlannedNotice:
    correction_id: int
    chat_id: int
    version_number: int
    operations: list[AnyOperation]


@dataclass(frozen=True)
class CorrectionQuestionNotice:
    correction_id: int
    chat_id: int
    version_number: int
    issues: list[Issue]
    clarification: str | None


@dataclass(frozen=True)
class CorrectionFailedNotice:
    correction_id: int
    chat_id: int
    error: str


Notice = (
    ProcessingNotice
    | PreviewNotice
    | FailureNotice
    | AdminJobNotice
    | CorrectionPlannedNotice
    | CorrectionQuestionNotice
    | CorrectionFailedNotice
)

_PLAN = TypeAdapter(CorrectionPlan)
_ISSUES = TypeAdapter(list[Issue])


def pending_notices(session: Session, app: Settings) -> list[Notice]:
    notices: list[Notice] = []
    for p in session.scalars(
        select(Project).where(
            Project.status == ProjectStatus.PROCESSING,
            Project.telegram_chat_id.is_not(None),
            Project.notified_status.is_distinct_from(ProjectStatus.PROCESSING.value),
        )
    ):
        assert p.telegram_chat_id is not None
        notices.append(ProcessingNotice(p.id, p.telegram_chat_id, p.status_message_id))

    ready = session.execute(
        select(Version, Project)
        .join(Project, Project.id == Version.project_id)
        .where(
            Version.status == VersionStatus.READY,
            Version.preview_sent_at.is_(None),
            Version.export_path.is_not(None),
            Project.telegram_chat_id.is_not(None),
            Project.approved_at.is_(None),
        )
        .order_by(Version.id)
    ).all()
    for v, p in ready:
        assert p.telegram_chat_id is not None and v.export_path is not None
        paths = ProjectPaths.for_project(app.data_dir, p.client_id, p.id).version(v.number)
        segments = (
            _SEGMENTS.validate_json(paths.segments.read_text(encoding="utf-8"))
            if paths.segments.exists()
            else []
        )
        removed = (
            _TEXTS.validate_json(paths.removed_segments.read_text(encoding="utf-8"))
            if paths.removed_segments.exists()
            else []
        )
        output = (v.cut_plan or {}).get("keep")
        duration = sum(r["end"] - r["start"] for r in output) if output else None
        notices.append(
            PreviewNotice(
                p.id,
                v.id,
                v.number,
                p.telegram_chat_id,
                p.status_message_id,
                app.data_dir / v.export_path,
                segments,
                duration,
                removed,
            )
        )

    failed = session.execute(
        select(Version, Project)
        .join(Project, Project.id == Version.project_id)
        .where(
            Version.status == VersionStatus.FAILED,
            Version.failure_notified_at.is_(None),
            Project.telegram_chat_id.is_not(None),
        )
        .order_by(Version.id)
    ).all()
    for v, p in failed:
        assert p.telegram_chat_id is not None
        job = session.scalar(
            select(Job)
            .where(Job.version_id == v.id, Job.status == JobStatus.FAILED)
            .order_by(Job.id.desc())
            .limit(1)
        )
        metrics = job.metrics if job else {}
        notices.append(
            FailureNotice(
                p.id,
                v.id,
                p.telegram_chat_id,
                p.status_message_id,
                metrics.get("error_code"),
                metrics.get("error_details") or {},
            )
        )

    corrections = session.execute(
        select(Correction, Version, Project)
        .join(Version, Version.id == Correction.version_id)
        .join(Project, Project.id == Version.project_id)
        .where(
            Correction.notified_at.is_(None),
            Correction.status.in_(
                [
                    CorrectionStatus.PLANNED,
                    CorrectionStatus.NEEDS_CLARIFICATION,
                    CorrectionStatus.REJECTED,
                ]
            ),
            Project.telegram_chat_id.is_not(None),
        )
        .order_by(Correction.id)
    ).all()
    for c, v, p in corrections:
        assert p.telegram_chat_id is not None
        plan = c.plan or {}
        if c.status is CorrectionStatus.PLANNED:
            ops = _PLAN.validate_python({"operations": plan.get("operations", [])}).operations
            notices.append(CorrectionPlannedNotice(c.id, p.telegram_chat_id, v.number, list(ops)))
        elif c.status is CorrectionStatus.NEEDS_CLARIFICATION:
            notices.append(
                CorrectionQuestionNotice(
                    c.id,
                    p.telegram_chat_id,
                    v.number,
                    _ISSUES.validate_python(plan.get("issues", [])),
                    plan.get("clarification"),
                )
            )
        elif "error" in plan:  # rejected by the worker, not by the client
            notices.append(CorrectionFailedNotice(c.id, p.telegram_chat_id, str(plan["error"])))

    jobs = session.execute(
        select(Job, Version, Project, Client)
        .outerjoin(Version, Version.id == Job.version_id)
        .outerjoin(Project, Project.id == Version.project_id)
        .outerjoin(Client, Client.id == Project.client_id)
        .where(Job.status == JobStatus.FAILED, Job.admin_notified_at.is_(None))
        .order_by(Job.id)
    ).all()
    for job, version, project, client in jobs:
        notices.append(
            AdminJobNotice(
                job.id,
                job.step,
                job.attempts,
                job.error or "",
                client.name if client else None,
                project.id if project else None,
                version.number if version else None,
            )
        )
    return notices


def mark_notice_sent(session: Session, notice: Notice, message_id: int | None = None) -> None:
    now = datetime.now(UTC)
    match notice:
        case ProcessingNotice():
            project = session.get(Project, notice.project_id)
            if project is not None:
                project.notified_status = ProjectStatus.PROCESSING.value
                if project.status_message_id is None:
                    project.status_message_id = message_id
        case PreviewNotice():
            version = session.get(Version, notice.version_id)
            project = session.get(Project, notice.project_id)
            if version is not None:
                version.preview_sent_at = now
                version.preview_message_id = message_id
            if project is not None:
                project.notified_status = ProjectStatus.REVIEW.value
        case FailureNotice():
            version = session.get(Version, notice.version_id)
            project = session.get(Project, notice.project_id)
            if version is not None:
                version.failure_notified_at = now
            if project is not None:
                project.notified_status = ProjectStatus.FAILED.value
        case AdminJobNotice():
            job = session.get(Job, notice.job_id)
            if job is not None:
                job.admin_notified_at = now
        case CorrectionPlannedNotice() | CorrectionQuestionNotice() | CorrectionFailedNotice():
            correction = session.get(Correction, notice.correction_id)
            if correction is not None:
                correction.notified_at = now
                correction.summary_message_id = message_id


# --- buttons ------------------------------------------------------------------


class Decision(StrEnum):
    OK = "ok"  # approved, or change request accepted
    ALREADY_APPROVED = "already_approved"
    OUTDATED = "outdated"
    NOT_READY = "not_ready"
    FORBIDDEN = "forbidden"


@dataclass(frozen=True)
class ButtonResult:
    decision: Decision
    export: Path | None = None
    project_id: int | None = None
    version_number: int | None = None
    client_name: str | None = None
    preview_message_id: int | None = None


def _check_button(
    session: Session, version_id: int, telegram_user_id: int
) -> tuple[ButtonResult, Version | None, Project | None]:
    """Shared checks: only the client's own version, only the newest, nothing approved yet."""
    row = session.execute(
        select(Version, Project, Client)
        .join(Project, Project.id == Version.project_id)
        .join(Client, Client.id == Project.client_id)
        .join(ClientUser, ClientUser.client_id == Client.id)
        .where(Version.id == version_id, ClientUser.telegram_user_id == telegram_user_id)
    ).first()
    if row is None:
        return ButtonResult(Decision.FORBIDDEN), None, None
    version, project, client = row[0], row[1], row[2]
    latest = session.scalar(
        select(func.max(Version.number)).where(Version.project_id == project.id)
    )
    if project.approved_at is not None:
        decision = Decision.ALREADY_APPROVED
    elif version.number != latest:
        decision = Decision.OUTDATED
    elif version.status is not VersionStatus.READY or version.export_path is None:
        decision = Decision.NOT_READY
    else:
        decision = Decision.OK
    result = ButtonResult(
        decision,
        project_id=project.id,
        version_number=version.number,
        client_name=client.name,
        preview_message_id=version.preview_message_id,
    )
    return result, version, project


def approve(
    session: Session, version_id: int, telegram_user_id: int, app: Settings
) -> ButtonResult:
    """Approve the newest ready version of the client's own project, once."""
    result, version, project = _check_button(session, version_id, telegram_user_id)
    if result.decision is not Decision.OK or version is None or project is None:
        return result
    assert version.export_path is not None
    now = datetime.now(UTC)
    project.approved_at = now
    project.status = ProjectStatus.APPROVED
    project.notified_status = ProjectStatus.APPROVED.value
    version.approved_at = now
    return replace(result, export=app.data_dir / version.export_path)


def change_request(session: Session, version_id: int, telegram_user_id: int) -> ButtonResult:
    """'Ändern' pressed. Free-text corrections come with Etappe 4; until then the admin is told."""
    result, _, _ = _check_button(session, version_id, telegram_user_id)
    return result


# --- corrections -------------------------------------------------------------------


class CorrectionStart(StrEnum):
    STARTED = "started"
    NO_PROJECT = "no_project"
    OUTDATED = "outdated"
    BUSY = "busy"
    APPROVED = "approved"
    DUPLICATE = "duplicate"


def _target_version(
    session: Session, access: Access, chat_id: int, reply_to: int | None
) -> tuple[Version, Project] | None:
    """The version a correction text refers to: the preview it replies to, else the newest
    project in this chat that has a preview."""
    base = (
        select(Version, Project)
        .join(Project, Project.id == Version.project_id)
        .where(Project.client_id == access.client_id, Project.telegram_chat_id == chat_id)
    )
    if reply_to is not None:
        row = session.execute(base.where(Version.preview_message_id == reply_to)).first()
        if row is None:
            row = session.execute(
                base.join(Correction, Correction.version_id == Version.id).where(
                    Correction.summary_message_id == reply_to
                )
            ).first()
        if row is not None:
            return row[0], row[1]
    row = session.execute(
        base.where(Version.preview_sent_at.is_not(None)).order_by(Version.id.desc()).limit(1)
    ).first()
    return (row[0], row[1]) if row else None


def start_correction(
    session: Session,
    access: Access,
    *,
    chat_id: int,
    message_id: int,
    text: str,
    reply_to: int | None,
) -> CorrectionStart:
    """Register a free-text correction and queue its interpretation for the worker."""
    target = _target_version(session, access, chat_id, reply_to)
    if target is None:
        return CorrectionStart.NO_PROJECT
    version, project = target
    if session.scalar(
        select(Correction.id)
        .join(Version, Version.id == Correction.version_id)
        .where(Version.project_id == project.id, Correction.telegram_message_id == message_id)
    ):
        return CorrectionStart.DUPLICATE
    if project.approved_at is not None:
        return CorrectionStart.APPROVED
    latest = session.scalar(
        select(func.max(Version.number)).where(Version.project_id == project.id)
    )
    if version.number != latest:
        newest = session.scalar(
            select(Version).where(Version.project_id == project.id, Version.number == latest)
        )
        # A reply to an old preview while a newer one exists: refuse (4.6).
        if reply_to is not None or newest is None or newest.preview_sent_at is not None:
            return CorrectionStart.OUTDATED
        return CorrectionStart.BUSY  # the newest version is still being rendered
    if version.status is not VersionStatus.READY:
        return CorrectionStart.BUSY

    previous = session.scalar(
        select(Correction).where(Correction.version_id == version.id).order_by(Correction.id.desc())
    )
    message_text = text.strip()
    now = datetime.now(UTC)
    if previous is not None:
        if previous.status is CorrectionStatus.RECEIVED:
            return CorrectionStart.BUSY
        if previous.status is CorrectionStatus.NEEDS_CLARIFICATION:
            # The answer to our question completes the earlier request.
            message_text = f"{previous.message_text}\nNachtrag: {message_text}"
        if previous.status in (CorrectionStatus.PLANNED, CorrectionStatus.NEEDS_CLARIFICATION):
            previous.status = CorrectionStatus.REJECTED  # superseded by the new message
            previous.notified_at = previous.notified_at or now

    if queue.enqueue(session, version.id, CORRECTION_STEP) is None:
        return CorrectionStart.BUSY
    session.add(
        Correction(
            version_id=version.id,
            message_text=message_text,
            telegram_message_id=message_id,
            status=CorrectionStatus.RECEIVED,
        )
    )
    return CorrectionStart.STARTED


@dataclass(frozen=True)
class CorrectionResult:
    decision: Decision
    new_version: int | None = None
    notes: tuple[str, ...] = ()
    client_name: str | None = None
    project_id: int | None = None
    base_version: int | None = None


def _correction_for_user(
    session: Session, correction_id: int, telegram_user_id: int
) -> tuple[Correction, ButtonResult, Version | None] | None:
    correction = session.get(Correction, correction_id)
    if correction is None:
        return None
    result, version, _ = _check_button(session, correction.version_id, telegram_user_id)
    if result.decision is Decision.FORBIDDEN:
        return None
    return correction, result, version


def apply_correction(
    session: Session, correction_id: int, telegram_user_id: int
) -> CorrectionResult:
    """'Ausführen': create the next version from the confirmed plan. Never without this click."""
    found = _correction_for_user(session, correction_id, telegram_user_id)
    if found is None:
        return CorrectionResult(Decision.FORBIDDEN)
    correction, check, base = found
    info = {
        "client_name": check.client_name,
        "project_id": check.project_id,
        "base_version": check.version_number,
    }
    if correction.status is CorrectionStatus.APPLIED:
        return CorrectionResult(Decision.ALREADY_APPROVED, **info)  # type: ignore[arg-type]
    if correction.status is not CorrectionStatus.PLANNED:
        return CorrectionResult(Decision.OUTDATED, **info)  # type: ignore[arg-type]
    if check.decision is not Decision.OK or base is None:
        return CorrectionResult(check.decision, **info)  # type: ignore[arg-type]

    plan = correction.plan or {}
    resolved = [Resolved.model_validate(r) for r in plan.get("resolved", [])]
    notes = tuple(r.op.text for r in resolved if isinstance(r.op, Note))
    step = plan.get("rerun_step")
    new_number = None
    if step:
        row = session.get(TranscriptRow, base.transcript_id)
        assert row is not None
        words = [Word.model_validate(w) for w in row.words]
        state = apply_ops(resolved, words, version_state(base))
        new = create_next_version(session, base, state, step)
        correction.result_version_id = new.id
        new_number = new.number
        project = session.get(Project, base.project_id)
        assert project is not None
        project.status_message_id = None  # a fresh status message for the new version
        project.notified_status = ProjectStatus.REVIEW.value
    correction.status = CorrectionStatus.APPLIED
    return CorrectionResult(Decision.OK, new_number, notes, **info)  # type: ignore[arg-type]


def reject_correction(
    session: Session, correction_id: int, telegram_user_id: int
) -> CorrectionResult:
    """'Nochmal': drop the plan; the client writes the request again."""
    found = _correction_for_user(session, correction_id, telegram_user_id)
    if found is None:
        return CorrectionResult(Decision.FORBIDDEN)
    correction, _, _ = found
    if correction.status not in (CorrectionStatus.PLANNED, CorrectionStatus.NEEDS_CLARIFICATION):
        return CorrectionResult(Decision.OUTDATED)
    correction.status = CorrectionStatus.REJECTED
    correction.notified_at = correction.notified_at or datetime.now(UTC)
    return CorrectionResult(Decision.OK)


# --- admin --------------------------------------------------------------------


def admin_create_client(session: Session, name: str, telegram_user_ids: Sequence[int]) -> Client:
    return create_client(session, name, telegram_user_ids=telegram_user_ids)


def admin_add_user(session: Session, client_ref: str, telegram_user_id: int) -> Client:
    client = get_client(session, client_ref)
    if client is None:
        raise ClientError(f"unknown client {client_ref!r}")
    owner = session.scalar(
        select(ClientUser).where(ClientUser.telegram_user_id == telegram_user_id)
    )
    if owner is not None:
        raise ClientError(f"telegram user {telegram_user_id} already belongs to a client")
    session.add(ClientUser(client_id=client.id, telegram_user_id=telegram_user_id))
    session.flush()
    return client


@dataclass(frozen=True)
class StatusSummary:
    jobs: dict[str, int]
    failed_24h: int
    projects: dict[str, int]
    oldest_queued_s: float | None


def status_summary(session: Session) -> StatusSummary:
    jobs = {s.value: n for s, n in session.execute(
        select(Job.status, func.count()).group_by(Job.status)
    ).all()}  # fmt: skip
    projects = {s.value: n for s, n in session.execute(
        select(Project.status, func.count()).group_by(Project.status)
    ).all()}  # fmt: skip
    since = datetime.now(UTC) - timedelta(hours=24)
    failed_24h = (
        session.scalar(
            select(func.count())
            .select_from(Job)
            .where(Job.status == JobStatus.FAILED, Job.updated_at > since)
        )
        or 0
    )
    oldest = session.scalar(select(func.min(Job.created_at)).where(Job.status == JobStatus.QUEUED))
    age = (datetime.now(UTC) - oldest).total_seconds() if oldest else None
    return StatusSummary(jobs, failed_24h, projects, age)
