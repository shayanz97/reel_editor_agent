"""Database side of the bot (synchronous, no Telegram types; called via asyncio.to_thread).

The bot never processes media. It registers incoming videos as projects (the worker takes
over) and reads back what changed, so it can tell the client.
"""

from __future__ import annotations

import re
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
from reels.db.glossary import add_term
from reels.db.models import (
    BrollAsset,
    BrollPlacement,
    BrollSource,
    Client,
    ClientUser,
    Correction,
    CorrectionStatus,
    GlossaryStatus,
    Job,
    JobStatus,
    PlacementStatus,
    Project,
    ProjectStatus,
    PublishPackage,
    PublishStatus,
    Version,
    VersionStatus,
)
from reels.db.models import Transcript as TranscriptRow
from reels.domain.corrections import (
    AnyOperation,
    CorrectionPlan,
    Issue,
    Note,
    ReplaceText,
    Resolved,
)
from reels.domain.corrections import apply as apply_ops
from reels.domain.glossary import GlossaryTerm, apply_glossary
from reels.domain.segments import Segment
from reels.domain.vocab import VocabQuestion
from reels.domain.words import Word, normalize_token
from reels.pipeline.runner import (
    CORRECTION_STEP,
    POST_REVISE_STEP,
    create_next_version,
    create_project,
    next_step,
    start_publishing,
    version_state,
)
from reels.pipeline.steps import ProjectPaths

VOCAB_STEP = "vocab"
_SEGMENTS = TypeAdapter(list[Segment])
_EDGE = re.compile(r"^[^\w]+|[^\w]+$", re.UNICODE)
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
    broll: list[str] = field(default_factory=list)  # "[B1] Satz 4 · Serverraum (Pixabay)"


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


@dataclass(frozen=True)
class PublishOfferNotice:
    package_id: int
    chat_id: int
    covers: list[tuple[int, Path, str]]  # number, png, headline
    text: str
    revision: int


@dataclass(frozen=True)
class PublishTextNotice:
    package_id: int
    chat_id: int
    text: str
    revision: int
    failed: bool  # the requested change did not work; this is the unchanged text


@dataclass(frozen=True)
class PublishFinalNotice:
    package_id: int
    chat_id: int
    cover: Path
    text: str


@dataclass(frozen=True)
class PublishFailedNotice:
    package_id: int
    chat_id: int


@dataclass(frozen=True)
class VocabNotice:
    version_id: int
    chat_id: int
    questions: list[VocabQuestion]


Notice = (
    PublishOfferNotice
    | PublishTextNotice
    | PublishFinalNotice
    | PublishFailedNotice
    | VocabNotice
    | ProcessingNotice
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
                _broll_lines(session, v.id, segments),
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

    packages = session.execute(
        select(PublishPackage, Project)
        .join(Version, Version.id == PublishPackage.version_id)
        .join(Project, Project.id == Version.project_id)
        .where(Project.telegram_chat_id.is_not(None), PublishPackage.final_sent_at.is_(None))
        .order_by(PublishPackage.id)
    ).all()
    for pkg, p in packages:
        assert p.telegram_chat_id is not None
        chat = p.telegram_chat_id
        if pkg.status is PublishStatus.FAILED:
            if pkg.failure_notified_at is None:
                notices.append(PublishFailedNotice(pkg.id, chat))
            continue
        if pkg.status is not PublishStatus.READY:
            continue
        text = pkg.post_text or ""
        if pkg.offer_sent_at is None:
            cover_list = [
                (c["number"], app.data_dir / c["path"], c["headline"])
                for c in pkg.covers
                if "path" in c
            ]
            notices.append(PublishOfferNotice(pkg.id, chat, cover_list, text, pkg.revision))
        elif pkg.revision > pkg.text_sent_revision:
            revision_failed = bool(pkg.post.get("revision_failed"))
            notices.append(PublishTextNotice(pkg.id, chat, text, pkg.revision, revision_failed))
        elif (
            pkg.selected_cover is not None
            and pkg.text_confirmed_at is not None
            and pkg.revision_request is None
        ):
            cover = next(c for c in pkg.covers if c["number"] == pkg.selected_cover)
            notices.append(PublishFinalNotice(pkg.id, chat, app.data_dir / cover["path"], text))

    waiting = session.execute(
        select(Version, Project)
        .join(Project, Project.id == Version.project_id)
        .where(Version.status == VersionStatus.PENDING, Project.telegram_chat_id.is_not(None))
        .order_by(Version.id)
    ).all()
    for v, p in waiting:
        assert p.telegram_chat_id is not None
        state = v.edits.get("vocab")
        if not state or state.get("notified_at") or not state.get("questions"):
            continue
        questions = [VocabQuestion.model_validate(q) for q in state["questions"]]
        notices.append(VocabNotice(v.id, p.telegram_chat_id, questions))

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


_SOURCE_LABEL = {
    BrollSource.PIXABAY: "Pixabay",
    BrollSource.TEXTCARD: "Textkarte",
    BrollSource.LIBRARY: "eigener Clip",
}
MAX_IDEA_CHARS = 40


def _broll_lines(session: Session, version_id: int, segments: Sequence[Segment]) -> list[str]:
    """Ready B-roll of a version, with the sentence it belongs to and where it comes from."""
    rows = session.execute(
        select(BrollPlacement, BrollAsset)
        .join(BrollAsset, BrollAsset.id == BrollPlacement.asset_id)
        .where(
            BrollPlacement.version_id == version_id,
            BrollPlacement.status == PlacementStatus.READY,
        )
        .order_by(BrollPlacement.number)
    ).all()
    lines = []
    for placement, asset in rows:
        segment = next(
            (s for s in segments if s.first_word <= placement.first_word <= s.last_word), None
        )
        if segment is None:
            continue  # its words were cut: not in the video
        idea = placement.card_text if asset.source is BrollSource.TEXTCARD else placement.idea
        idea = (idea or "").strip().rstrip(".")
        if len(idea) > MAX_IDEA_CHARS:
            idea = idea[: MAX_IDEA_CHARS - 1].rstrip() + "…"
        lines.append(
            f"[B{placement.number}] Satz {segment.number} · {idea} ({_SOURCE_LABEL[asset.source]})"
        )
    return lines


def mark_notice_sent(
    session: Session, notice: Notice, message_id: int | dict[int, int] | None = None
) -> None:
    """`message_id`: the sent message, or {question id: message id} for vocab questions."""
    now = datetime.now(UTC)
    if isinstance(notice, VocabNotice):
        version = session.get(Version, notice.version_id)
        if version is not None:
            state = dict(version.edits.get("vocab", {}))
            ids = message_id if isinstance(message_id, dict) else {}
            state["questions"] = [
                {**q, "message_id": ids.get(q["id"], q.get("message_id"))}
                for q in state.get("questions", [])
            ]
            state["notified_at"] = now.isoformat()
            version.edits = {**version.edits, "vocab": state}
        return
    if isinstance(message_id, dict):
        message_id = None
    if isinstance(
        notice, PublishOfferNotice | PublishTextNotice | PublishFinalNotice | PublishFailedNotice
    ):
        package = session.get(PublishPackage, notice.package_id)
        if package is None:
            return
        match notice:
            case PublishOfferNotice():
                package.offer_sent_at = now
                package.text_message_id = message_id
                package.text_sent_revision = notice.revision
            case PublishTextNotice():
                package.text_message_id = message_id
                package.text_sent_revision = notice.revision
                package.post = {**package.post, "revision_failed": False}
            case PublishFinalNotice():
                package.final_sent_at = now
                package.status = PublishStatus.DONE
            case PublishFailedNotice():
                package.failure_notified_at = now
        return
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
    start_publishing(session, version.id)
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
class GlossaryCandidate:
    index: int  # position of the replace_text operation in the plan
    find: str
    replace: str


@dataclass(frozen=True)
class CorrectionResult:
    decision: Decision
    new_version: int | None = None
    notes: tuple[str, ...] = ()
    glossary: tuple[GlossaryCandidate, ...] = ()
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
    candidates = tuple(
        GlossaryCandidate(i, r.op.find, r.op.replace)
        for i, r in enumerate(resolved)
        if isinstance(r.op, ReplaceText) and is_glossary_candidate(r.op.find, r.op.replace)
    )
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
    return CorrectionResult(Decision.OK, new_number, notes, candidates, **info)  # type: ignore[arg-type]


MAX_GLOSSARY_TERM_WORDS = 4


def is_glossary_candidate(find: str, replace: str) -> bool:
    """A spelling fix worth remembering: different words, short, not just punctuation/case."""
    a = [normalize_token(t) for t in find.split()]
    b = [normalize_token(t) for t in replace.split()]
    a, b = [t for t in a if t], [t for t in b if t]
    return bool(a) and bool(b) and a != b and len(b) <= MAX_GLOSSARY_TERM_WORDS


@dataclass(frozen=True)
class GlossaryAnswer:
    decision: Decision
    find: str | None = None
    replace: str | None = None


def correction_to_glossary(
    session: Session, correction_id: int, index: int, telegram_user_id: int, *, add: bool
) -> GlossaryAnswer:
    """4.7: the client answered „Soll das künftig immer so korrigiert werden?“."""
    row = session.execute(
        select(Correction, Project)
        .join(Version, Version.id == Correction.version_id)
        .join(Project, Project.id == Version.project_id)
        .join(ClientUser, ClientUser.client_id == Project.client_id)
        .where(Correction.id == correction_id, ClientUser.telegram_user_id == telegram_user_id)
    ).first()
    if row is None:
        return GlossaryAnswer(Decision.FORBIDDEN)
    correction, project = row[0], row[1]
    resolved = (correction.plan or {}).get("resolved", [])
    if correction.status is not CorrectionStatus.APPLIED or not 0 <= index < len(resolved):
        return GlossaryAnswer(Decision.OUTDATED)
    op = Resolved.model_validate(resolved[index]).op
    if not isinstance(op, ReplaceText) or not is_glossary_candidate(op.find, op.replace):
        return GlossaryAnswer(Decision.OUTDATED)
    term = _EDGE.sub("", op.replace.strip())
    if add:
        add_term(session, project.client_id, term, [op.find], source="correction")
    return GlossaryAnswer(Decision.OK, op.find, term)


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


# --- vocabulary questions (4.8) ---------------------------------------------------


@dataclass(frozen=True)
class VocabTarget:
    version_id: int
    question_id: int | None  # None: questions are open, but the text answers none of them


def vocab_target(
    session: Session, access: Access, chat_id: int, reply_to: int | None
) -> VocabTarget | None:
    """Is a text message an answer to a vocabulary question? None if no question is open."""
    rows = session.scalars(
        select(Version)
        .join(Project, Project.id == Version.project_id)
        .where(
            Project.client_id == access.client_id,
            Project.telegram_chat_id == chat_id,
            Version.status == VersionStatus.PENDING,
        )
        .order_by(Version.id.desc())
    ).all()
    open_version: int | None = None
    for v in rows:
        for q in v.edits.get("vocab", {}).get("questions", []):
            if q.get("answer") is not None:
                continue
            if reply_to is not None and q.get("message_id") == reply_to:
                return VocabTarget(v.id, q["id"])
            open_version = open_version or v.id
    return VocabTarget(open_version, None) if open_version is not None else None


@dataclass(frozen=True)
class VocabAnswer:
    decision: Decision
    question: str | None = None
    answer: str | None = None  # None: keep as transcribed
    finished: bool = False


def answer_vocab(
    session: Session,
    version_id: int,
    question_id: int,
    telegram_user_id: int,
    *,
    keep: bool = False,
    use_suggestion: bool = False,
    spelling: str | None = None,
) -> VocabAnswer:
    """Record one answer. When the last one is in: apply them and let the pipeline continue."""
    row = session.execute(
        select(Version, Project)
        .join(Project, Project.id == Version.project_id)
        .join(ClientUser, ClientUser.client_id == Project.client_id)
        .where(Version.id == version_id, ClientUser.telegram_user_id == telegram_user_id)
        .with_for_update(of=Version)
    ).first()
    if row is None:
        return VocabAnswer(Decision.FORBIDDEN)
    version, project = row[0], row[1]
    state = dict(version.edits.get("vocab", {}))
    questions = [VocabQuestion.model_validate(q) for q in state.get("questions", [])]
    question = next((q for q in questions if q.id == question_id), None)
    if question is None or question.answer is not None:
        return VocabAnswer(Decision.ALREADY_APPROVED)
    if use_suggestion and question.suggestion:
        answer = question.suggestion
    elif spelling and spelling.strip():
        answer = spelling.strip()
    else:
        answer = None
    question.answer = answer if answer is not None else "keep"
    state["questions"] = [q.model_dump(mode="json") for q in questions]
    version.edits = {**version.edits, "vocab": state}

    finished = all(q.answer is not None for q in questions)
    if finished:
        _apply_vocab_answers(session, version, project, questions)
    return VocabAnswer(Decision.OK, question.text, answer, finished)


def _apply_vocab_answers(
    session: Session, version: Version, project: Project, questions: Sequence[VocabQuestion]
) -> None:
    """Corrected spellings go into the text overlay (all occurrences) and into the glossary as
    suggestions (they take effect only once confirmed). Then the next step is queued."""
    fixes = [q for q in questions if q.answer not in (None, "keep")]
    if fixes:
        row = session.get(TranscriptRow, version.transcript_id)
        assert row is not None
        words = [Word.model_validate(w) for w in row.words]
        overlay = {int(k): v for k, v in version.text_overlay.items()}
        for q in fixes:
            assert q.answer is not None
            term = GlossaryTerm(term=q.answer, variants=[q.text])
            overlay, _ = apply_glossary(words, [term], overlay)
            add_term(
                session,
                project.client_id,
                q.answer,
                [q.text],
                status=GlossaryStatus.SUGGESTED,
                source="vocab",
            )
        version.text_overlay = {str(k): v for k, v in sorted(overlay.items())}
    following = next_step(VOCAB_STEP)
    assert following is not None
    queue.enqueue(session, version.id, following)


# --- publish package (5.7) ------------------------------------------------------------


def _package_for_user(
    session: Session, package_id: int, telegram_user_id: int
) -> PublishPackage | None:
    return session.scalar(
        select(PublishPackage)
        .join(Version, Version.id == PublishPackage.version_id)
        .join(Project, Project.id == Version.project_id)
        .join(ClientUser, ClientUser.client_id == Project.client_id)
        .where(PublishPackage.id == package_id, ClientUser.telegram_user_id == telegram_user_id)
        .with_for_update(of=PublishPackage)
    )


@dataclass(frozen=True)
class PublishChoice:
    decision: Decision
    cover_chosen: bool = False
    text_confirmed: bool = False


def _choice(decision: Decision, package: PublishPackage) -> PublishChoice:
    return PublishChoice(
        decision, package.selected_cover is not None, package.text_confirmed_at is not None
    )


def select_cover(
    session: Session, package_id: int, number: int, telegram_user_id: int
) -> PublishChoice:
    package = _package_for_user(session, package_id, telegram_user_id)
    if package is None:
        return PublishChoice(Decision.FORBIDDEN)
    if package.final_sent_at is not None:
        return _choice(Decision.ALREADY_APPROVED, package)
    if not any(c["number"] == number for c in package.covers):
        return _choice(Decision.OUTDATED, package)
    package.selected_cover = number
    return _choice(Decision.OK, package)


def confirm_text(session: Session, package_id: int, telegram_user_id: int) -> PublishChoice:
    package = _package_for_user(session, package_id, telegram_user_id)
    if package is None:
        return PublishChoice(Decision.FORBIDDEN)
    if package.final_sent_at is not None:
        return _choice(Decision.ALREADY_APPROVED, package)
    if package.revision_request is not None:
        return _choice(Decision.NOT_READY, package)  # a change is being made right now
    package.text_confirmed_at = datetime.now(UTC)
    return _choice(Decision.OK, package)


def publish_text_target(
    session: Session, access: Access, chat_id: int, reply_to: int | None
) -> int | None:
    """Package whose post text a free-text message refers to (reply, or the open one)."""
    base = (
        select(PublishPackage)
        .join(Version, Version.id == PublishPackage.version_id)
        .join(Project, Project.id == Version.project_id)
        .where(
            Project.client_id == access.client_id,
            Project.telegram_chat_id == chat_id,
            PublishPackage.status == PublishStatus.READY,
            PublishPackage.final_sent_at.is_(None),
        )
    )
    if reply_to is not None:
        package = session.scalar(base.where(PublishPackage.text_message_id == reply_to))
        if package is not None:
            return package.id
    package = session.scalar(
        base.where(
            PublishPackage.offer_sent_at.is_not(None), PublishPackage.text_confirmed_at.is_(None)
        ).order_by(PublishPackage.id.desc())
    )
    return package.id if package is not None else None


def request_text_revision(
    session: Session, package_id: int, telegram_user_id: int, request: str
) -> Decision:
    package = _package_for_user(session, package_id, telegram_user_id)
    if package is None:
        return Decision.FORBIDDEN
    if package.final_sent_at is not None:
        return Decision.ALREADY_APPROVED
    if package.revision_request is not None:
        return Decision.NOT_READY
    if queue.enqueue(session, package.version_id, POST_REVISE_STEP) is None:
        return Decision.NOT_READY
    package.revision_request = request.strip()
    package.text_confirmed_at = None
    return Decision.OK


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
