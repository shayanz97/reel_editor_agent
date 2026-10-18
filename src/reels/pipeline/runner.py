"""Pipeline as jobs: one job per step, chained in a fixed order.

A job's step work, its completion and the enqueueing of the next step are committed in ONE
transaction while the job row is locked. A crash therefore never leaves half a result behind,
and a restarted worker simply runs the step again (every step is idempotent).
"""

from __future__ import annotations

import time
from collections import Counter
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol

from sqlalchemy import Engine, delete, func, select
from sqlalchemy.orm import Session

from reels.broll import library, planner, textcards
from reels.broll.check import check_image
from reels.broll.pixabay import PixabayClient, StockUnavailableError
from reels.config import Settings, get_settings
from reels.db import queue
from reels.db.glossary import add_hits, load_terms
from reels.db.models import (
    Asset,
    AssetKind,
    BrollAsset,
    BrollPlacement,
    BrollSource,
    Client,
    Correction,
    CorrectionStatus,
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
from reels.db.models import (
    Transcript as TranscriptRow,
)
from reels.domain import vocab
from reels.domain.broll_rules import BrollRules, Candidate, word_span
from reels.domain.broll_rules import select as select_slots
from reels.domain.corrections import (
    AddBroll,
    RemoveBroll,
    ReplaceBroll,
    Resolved,
    SetBrollCoverage,
    VersionState,
    removed_segments,
    rerun_step,
    resolve,
)
from reels.domain.cutplan import CutPlan
from reels.domain.glossary import GlossaryTerm, apply_glossary_text, build_prompt_hint
from reels.domain.segments import Segment, Span, number_segments, split_sentences
from reels.domain.timeline import Timeline
from reels.domain.words import Transcript, display_text
from reels.llm.base import LLMError, LLMProvider, load_prompt
from reels.llm.schemas import (
    BrollPlanResponse,
    CorrectionResponse,
    CoverPlanResponse,
    PostDraft,
    VocabResponse,
)
from reels.log import get_logger, log_context
from reels.media import frames
from reels.media.ffmpeg import FFmpegError
from reels.media.probe import ProbeResult, VideoRejectedError
from reels.media.render import Overlay
from reels.pipeline import steps
from reels.pipeline.steps import ProjectPaths
from reels.publish import cover_plan, covers, post_text
from reels.settings_schema import ClientSettings, SubtitleSettings
from reels.transcribe.base import TranscriptionEngine
from reels.vad.silero import VoiceActivityDetector

log = get_logger(__name__)

STEP_ORDER: tuple[str, ...] = (
    "probe",
    "audio",
    "transcribe",
    "glossary",
    "vocab",
    "vad",
    "cutplan",
    "segments",
    "highlights",
    "broll_plan",
    "broll_fetch",
    "subtitles",
    "render",
)
# After approval: the Instagram package (covers, post text, hashtags).
PUBLISH_ORDER: tuple[str, ...] = ("frames", "cover_plan", "covers", "post_text")
POST_REVISE_STEP = "post_revise"
FRAME_SAMPLES = 24  # frames extracted and scored
FRAME_CANDIDATES = 6  # best ones shown to the vision model
CLEANUP_STEP = "cleanup"
CORRECTION_STEP = "correction"


class PermanentStepError(Exception):
    """Retrying cannot help (video too long, no speech). The job fails immediately.

    `code` and `details` end up in jobs.metrics so the bot can tell the client why.
    """

    def __init__(self, message: str, code: str = "permanent", **details: float) -> None:
        super().__init__(message)
        self.code = code
        self.details = details


class Notifier(Protocol):
    def notify_admin(self, text: str) -> None: ...


class LogNotifier:
    """Admin notifications as log lines until the Telegram admin chat exists (Etappe 3)."""

    def notify_admin(self, text: str) -> None:
        log.error("admin_notify", extra={"text": text})


@dataclass
class Providers:
    """Everything a step needs from the outside world; replaced by fakes in tests."""

    engine: Callable[[str], TranscriptionEngine]
    vad: Callable[[], VoiceActivityDetector]
    llm: Callable[[], LLMProvider | None]
    notifier: Notifier = field(default_factory=LogNotifier)
    stock: Callable[[], PixabayClient | None] = lambda: None


def default_providers() -> Providers:
    from reels.llm.openai_provider import OpenAIProvider
    from reels.transcribe.base import get_engine
    from reels.vad.silero import SileroVad

    def stock() -> PixabayClient | None:
        app = get_settings()
        if app.pixabay_api_key is None:
            return None
        return PixabayClient(
            app.pixabay_api_key.get_secret_value(), app.data_dir / "cache" / "pixabay"
        )

    return Providers(
        engine=get_engine,
        vad=SileroVad,
        llm=lambda: OpenAIProvider.from_settings(get_settings()),
        stock=stock,
    )


def next_step(step: str) -> str | None:
    for order in (STEP_ORDER, PUBLISH_ORDER):
        if step in order:
            i = order.index(step)
            return order[i + 1] if i + 1 < len(order) else None
    return None


# --- creating work ------------------------------------------------------------


def create_project(
    session: Session,
    client: Client,
    source: Path,
    app: Settings,
    *,
    title: str | None = None,
    caption: str | None = None,
    telegram_chat_id: int | None = None,
    telegram_message_id: int | None = None,
) -> Version:
    """Project + original asset + version 1, and queue the first step. Caller commits."""
    project = Project(
        client_id=client.id,
        status=ProjectStatus.RECEIVED,
        title=title or source.name,
        caption=caption,
        telegram_chat_id=telegram_chat_id,
        telegram_message_id=telegram_message_id,
    )
    session.add(project)
    session.flush()
    paths = ProjectPaths.for_project(app.data_dir, client.id, project.id)
    original = steps.store_original(source, paths)
    session.add(
        Asset(
            project_id=project.id,
            kind=AssetKind.ORIGINAL,
            path=_rel(original, app),
            size_bytes=original.stat().st_size,
        )
    )
    version = Version(project_id=project.id, number=1, status=VersionStatus.PENDING)
    session.add(version)
    session.flush()
    queue.enqueue(session, version.id, STEP_ORDER[0])
    return version


def rerun_from(session: Session, version_id: int, step: str) -> int | None:
    """Run a version again from `step` on (earlier results are reused). Caller commits.

    Publish steps (frames …) rebuild the Instagram package of an approved version.
    """
    if step not in STEP_ORDER and step not in PUBLISH_ORDER:
        steps_text = ", ".join((*STEP_ORDER, *PUBLISH_ORDER))
        raise ValueError(f"unknown step {step!r}, expected one of {steps_text}")
    version = session.get(Version, version_id)
    if version is None:
        raise ValueError(f"version {version_id} not found")
    if step in PUBLISH_ORDER:
        package = get_package(session, version_id)
        package.status = PublishStatus.PENDING
        return queue.enqueue(session, version_id, step)
    version.status = VersionStatus.PENDING
    return queue.enqueue(session, version_id, step)


def get_package(session: Session, version_id: int) -> PublishPackage:
    package = session.scalar(select(PublishPackage).where(PublishPackage.version_id == version_id))
    if package is None:
        package = PublishPackage(version_id=version_id, status=PublishStatus.PENDING)
        session.add(package)
        session.flush()
    return package


def start_publishing(session: Session, version_id: int) -> int | None:
    """After approval: create the package and queue its first step. Caller commits."""
    get_package(session, version_id)
    return queue.enqueue(session, version_id, PUBLISH_ORDER[0])


# --- executing ----------------------------------------------------------------


@dataclass
class StepContext:
    session: Session
    job: Job
    version: Version
    project: Project
    client: Client
    settings: ClientSettings
    app: Settings
    paths: ProjectPaths
    providers: Providers
    metrics: dict[str, Any] = field(default_factory=dict)
    # Set by a step that must wait for the client (vocab questions): the next step is not
    # queued now; the bot queues it once all answers are in.
    hold: bool = False

    def path(self, rel: str) -> Path:
        return self.app.data_dir / rel

    def rel(self, path: Path) -> str:
        return _rel(path, self.app)


def _rel(path: Path, app: Settings) -> str:
    return str(path.resolve().relative_to(app.data_dir.resolve()))


def execute_job(
    engine: Engine, job_id: int, token: str, providers: Providers, max_attempts: int = 3
) -> JobStatus | None:
    """Run one claimed job. Returns its new status, or None if the claim was no longer ours."""
    app = get_settings()
    with Session(engine, expire_on_commit=False) as session:
        job = queue.lock_for_execution(session, job_id, token)
        if job is None:
            log.warning("job_claim_lost", extra={"job_id": job_id})
            return None
        step, version_id = job.step, job.version_id
        version = session.get(Version, version_id) if version_id is not None else None
        project = session.get(Project, version.project_id) if version else None
        with log_context(
            job_id=job_id,
            version_id=version_id,
            project_id=project.id if project else None,
            client_id=project.client_id if project else None,
        ):
            start = time.perf_counter()
            try:
                hold = False
                if step == CLEANUP_STEP:
                    from reels.pipeline.cleanup import cleanup_expired

                    metrics = cleanup_expired(session, app.data_dir)
                elif step == library.INDEX_STEP:
                    metrics = library.index_pending(session, app, providers.llm())
                else:
                    if version is None or project is None:
                        raise PermanentStepError(f"job {job_id} has no version")
                    client = session.get(Client, project.client_id)
                    assert client is not None
                    ctx = StepContext(
                        session=session,
                        job=job,
                        version=version,
                        project=project,
                        client=client,
                        settings=client.client_settings(),
                        app=app,
                        paths=ProjectPaths.for_project(app.data_dir, client.id, project.id),
                        providers=providers,
                    )
                    ALL_STEPS[step](ctx)
                    metrics = ctx.metrics
                    hold = ctx.hold
                metrics["runtime_s"] = round(time.perf_counter() - start, 3)
                metrics["attempt"] = job.attempts
                queue.complete(session, job, metrics)
                following = next_step(step) if version_id is not None else None
                if following is not None and not hold:
                    queue.enqueue(session, version_id, following)
                session.commit()
            except Exception as exc:
                session.rollback()
                failure: dict[str, Any] = {"runtime_s": round(time.perf_counter() - start, 3)}
                if isinstance(exc, PermanentStepError):
                    failure.update(error_code=exc.code, error_details=exc.details)
                log.exception("step_failed", extra={"step": step})
                return handle_failure(
                    engine,
                    job_id,
                    _describe(exc),
                    providers.notifier,
                    permanent=isinstance(exc, PermanentStepError),
                    max_attempts=max_attempts,
                    metrics=failure,
                )
            log.info("step_done", extra={"step": step, "runtime_s": metrics["runtime_s"]})
            return JobStatus.DONE


def _describe(exc: BaseException) -> str:
    text = f"{type(exc).__name__}: {exc}"
    if isinstance(exc, FFmpegError):
        text += "\n" + exc.stderr_tail
    return text


def handle_failure(
    engine: Engine,
    job_id: int,
    error: str,
    notifier: Notifier,
    *,
    permanent: bool = False,
    max_attempts: int = 3,
    metrics: dict[str, Any] | None = None,
) -> JobStatus | None:
    """Count a failed attempt; on final failure mark version/project and tell the admin."""
    with Session(engine) as session:
        status = queue.fail(
            session,
            job_id,
            error,
            permanent=permanent,
            max_attempts=max_attempts,
            metrics=metrics,
        )
        job = session.get(Job, job_id)
        if status is JobStatus.FAILED and job is not None:
            mark_failed(session, job)
        session.commit()
        step = job.step if job else "?"
    if status is JobStatus.FAILED:
        cause = error.splitlines()[0][:300]
        notifier.notify_admin(f"Job {job_id} ({step}) endgültig fehlgeschlagen: {cause}")
    return status


def mark_failed(session: Session, job: Job) -> None:
    if job.version_id is None:
        return
    if job.step in PUBLISH_ORDER:
        # The approved video is fine; only its package could not be built.
        get_package(session, job.version_id).status = PublishStatus.FAILED
        return
    if job.step == POST_REVISE_STEP:
        # Keep the current text; the client is told the change did not work.
        package = get_package(session, job.version_id)
        package.revision_request = None
        package.post = {**package.post, "revision_failed": True}
        package.revision += 1
        return
    if job.step == CORRECTION_STEP:
        # The version itself is fine; only this correction could not be interpreted.
        correction = session.scalar(
            select(Correction)
            .where(
                Correction.version_id == job.version_id,
                Correction.status == CorrectionStatus.RECEIVED,
            )
            .order_by(Correction.id.desc())
            .limit(1)
        )
        if correction is not None:
            correction.status = CorrectionStatus.REJECTED
            correction.plan = {"error": "failed"}
        return
    version = session.get(Version, job.version_id)
    if version is None:
        return
    version.status = VersionStatus.FAILED
    project = session.get(Project, version.project_id)
    if project is not None:
        project.status = ProjectStatus.FAILED


# --- loaders for results of earlier steps -------------------------------------


def _original(ctx: StepContext) -> Path:
    rel = ctx.session.scalar(
        select(Asset.path).where(
            Asset.project_id == ctx.project.id, Asset.kind == AssetKind.ORIGINAL
        )
    )
    if rel is None:
        raise PermanentStepError("project has no original video", "no_original")
    return ctx.path(rel)


def _probe(ctx: StepContext) -> ProbeResult:
    return ProbeResult.model_validate_json(ctx.paths.probe.read_text(encoding="utf-8"))


def _transcript(ctx: StepContext) -> Transcript:
    if ctx.version.transcript_id is None:
        raise RuntimeError("version has no transcript (transcribe step missing)")
    row = ctx.session.get(TranscriptRow, ctx.version.transcript_id)
    assert row is not None
    return Transcript(engine=row.engine, model=row.model, language=row.language, words=row.words)


def version_state(version: Version) -> VersionState:
    return VersionState.from_version(version.text_overlay, version.highlights, version.edits)


def _structure(
    ctx: StepContext, transcript: Transcript
) -> tuple[Timeline, list[Span], list[Segment], VersionState]:
    """Timeline, spans and numbered segments of this version, with its edits applied."""
    state = version_state(ctx.version)
    timeline = Timeline(_plan(ctx).keep)
    spans, segments = steps.segments_step(transcript, timeline, state.overlay, state.splits_after)
    return timeline, spans, segments, state


def _plan(ctx: StepContext) -> CutPlan:
    if ctx.version.cut_plan is None:
        raise RuntimeError("version has no cut plan (cutplan step missing)")
    return CutPlan.model_validate(ctx.version.cut_plan)


def _mtime(path: Path) -> int | None:
    return path.stat().st_mtime_ns if path.exists() else None


# --- steps --------------------------------------------------------------------


def step_probe(ctx: StepContext) -> None:
    ctx.project.status = ProjectStatus.PROCESSING
    try:
        result = steps.probe_step(_original(ctx), ctx.paths, ctx.settings)
    except VideoRejectedError as exc:
        raise PermanentStepError(str(exc), exc.code, **exc.details) from exc
    ctx.metrics.update(
        duration_s=result.duration, width=result.width, height=result.height, fps=result.fps
    )


def step_audio(ctx: StepContext) -> None:
    before = _mtime(ctx.paths.audio)
    steps.audio_step(_original(ctx), ctx.paths)
    ctx.metrics["reused"] = before is not None and before == _mtime(ctx.paths.audio)


def step_transcribe(ctx: StepContext) -> None:
    engine = ctx.providers.engine(ctx.app.transcribe_engine)
    before = _mtime(ctx.paths.transcript)
    hint = build_prompt_hint(load_terms(ctx.session, ctx.client.id))
    transcript = steps.transcribe_step(ctx.paths, engine, hint)
    reused = before is not None and before == _mtime(ctx.paths.transcript)

    row = (
        ctx.session.get(TranscriptRow, ctx.version.transcript_id)
        if ctx.version.transcript_id
        else None
    )
    if row is None or row.engine != transcript.engine or row.model != transcript.model:
        row = TranscriptRow(
            project_id=ctx.project.id,
            engine=transcript.engine,
            model=transcript.model,
            language=transcript.language,
            words=[w.model_dump(mode="json") for w in transcript.words],
            params={"hint": hint} if hint else {},
        )
        ctx.session.add(row)
        ctx.session.flush()
        ctx.version.transcript_id = row.id
    ctx.metrics.update(
        reused=reused, engine=transcript.engine, model=transcript.model, words=len(transcript.words)
    )


def step_glossary(ctx: StepContext) -> None:
    transcript = _transcript(ctx)
    overlay, hits = steps.glossary_step(transcript, load_terms(ctx.session, ctx.client.id))
    ctx.version.text_overlay = {str(k): v for k, v in sorted(overlay.items())}
    # Count hits once per version, not again on every rerun.
    counted_before = ctx.session.scalar(
        select(func.count())
        .select_from(Job)
        .where(
            Job.version_id == ctx.version.id,
            Job.step == "glossary",
            Job.status == JobStatus.DONE,
        )
    )
    if not counted_before:
        add_hits(ctx.session, ctx.client.id, hits)
    ctx.metrics.update(hits=dict(hits), hits_counted=not counted_before)


def step_vocab(ctx: StepContext) -> None:
    """4.8: let the LLM flag probably misheard words; the client is asked before cutting."""
    existing = ctx.version.edits.get("vocab")
    if existing is not None:
        # Asked before (e.g. rerun): wait only if answers are still missing.
        open_questions = [q for q in existing.get("questions", []) if q.get("answer") is None]
        ctx.hold = bool(open_questions) and ctx.project.telegram_chat_id is not None
        ctx.metrics.update(reused=True, open_questions=len(open_questions))
        return
    llm = ctx.providers.llm()
    if llm is None:
        ctx.metrics.update(skipped="no_llm")
        return
    transcript = _transcript(ctx)
    state = version_state(ctx.version)
    spans = split_sentences(transcript.words)
    segments = number_segments(
        transcript.words, spans, {w.id for w in transcript.words}, state.overlay
    )
    terms = load_terms(ctx.session, ctx.client.id)
    prompt = load_prompt(
        "vocab",
        known=", ".join(t.term for t in terms) or "keine",
        caption=ctx.project.caption or "",
        transcript=vocab.transcript_lines(segments, transcript.words, state.overlay),
    )
    used_before = len(llm.usage)
    try:
        response = llm.structured("vocab", prompt, VocabResponse)
    except LLMError as exc:
        # The check helps, but must not block the video.
        log.warning("vocab_failed", extra={"error": str(exc)})
        ctx.metrics.update(skipped="llm_error")
        return
    items = [
        vocab.SuspiciousWord(
            segment=i.segment,
            first_word=i.first_word_id,
            last_word=i.last_word_id,
            suggestion=i.suggestion,
        )
        for i in response.items
    ]
    questions = vocab.select_questions(
        items, transcript.words, segments, state.overlay, vocab.known_tokens(terms)
    )
    ctx.version.edits = {
        **ctx.version.edits,
        "vocab": {
            "questions": [q.model_dump(mode="json") for q in questions],
            "notified_at": None,
        },
    }
    # Without a Telegram chat (CLI) nobody can answer: record, but do not wait.
    ctx.hold = bool(questions) and ctx.project.telegram_chat_id is not None
    calls = llm.usage[used_before:]
    ctx.metrics.update(
        flagged=len(response.items),
        asked=len(questions),
        waiting=ctx.hold,
        llm_input_tokens=sum(u.input_tokens for u in calls),
        llm_output_tokens=sum(u.output_tokens for u in calls),
    )


def step_vad(ctx: StepContext) -> None:
    before = _mtime(ctx.paths.vad)
    regions = steps.vad_step(ctx.paths, ctx.providers.vad())
    ctx.metrics.update(
        reused=before is not None and before == _mtime(ctx.paths.vad), regions=len(regions)
    )


def step_cutplan(ctx: StepContext) -> None:
    speech = steps.load_speech(ctx.paths)
    src = _probe(ctx)
    state = version_state(ctx.version)
    plan = steps.cutplan_step(
        _transcript(ctx),
        speech,
        src,
        ctx.settings,
        removed=state.removed_words,
        keep_pauses_after=state.keep_pauses_after,
    )
    if not plan.keep:
        raise PermanentStepError("no speech found in the video", "no_speech")
    ctx.version.cut_plan = plan.model_dump(mode="json")
    steps.write_json(ctx.paths.version(ctx.version.number).cutplan, plan)
    ctx.metrics.update(
        original_s=src.duration,
        output_s=round(plan.output_duration, 3),
        cuts=plan.cut_count,
        removed_words=int(plan.params["removed_words"]),
    )


def step_segments(ctx: StepContext) -> None:
    transcript = _transcript(ctx)
    _, spans, segments, state = _structure(ctx, transcript)
    vpaths = ctx.paths.version(ctx.version.number)
    steps.write_json(vpaths.segments, segments)
    # Segments removed by corrections, listed in the preview so they can be restored (E1 …).
    by_id = {w.id: w for w in transcript.words}
    removed = [
        " ".join(
            display_text(by_id[i], state.overlay)
            for i in range(span.first_word, span.last_word + 1)
            if display_text(by_id[i], state.overlay).strip()
        )
        for span in removed_segments(spans, state.removed_words)
    ]
    steps.write_json(vpaths.removed_segments, removed)
    ctx.metrics.update(segments=len(segments), removed_segments=len(removed))


def step_highlights(ctx: StepContext) -> None:
    if ctx.version.parent_id is not None:
        # Later versions keep their highlights (incl. the client's own highlight edits).
        ctx.metrics.update(highlights=len(ctx.version.highlights), reused=True, llm_used=False)
        return
    transcript = _transcript(ctx)
    timeline, _, segments, state = _structure(ctx, transcript)
    overlay = state.overlay
    llm = ctx.providers.llm()
    used_before = len(llm.usage) if llm else 0
    selection = steps.highlights_step(llm, transcript, segments, timeline, overlay)
    ctx.version.highlights = selection.word_ids
    calls = llm.usage[used_before:] if llm else []
    ctx.metrics.update(
        highlights=len(selection.word_ids),
        highlight_segments_missing=selection.missing_segments,
        highlight_choices_rejected=selection.rejected,
        llm_used=llm is not None,
        llm_calls=[
            {"task": u.task, "model": u.model, "in": u.input_tokens, "out": u.output_tokens}
            for u in calls
        ],
        llm_input_tokens=sum(u.input_tokens for u in calls),
        llm_output_tokens=sum(u.output_tokens for u in calls),
    )


# --- B-roll --------------------------------------------------------------------------------


def _broll_rules(settings: ClientSettings, state: VersionState | None = None) -> BrollRules:
    """Client rules; a version may override the coverage (correction set_broll_coverage)."""
    b = settings.broll
    coverage = b.max_coverage
    if state is not None and state.broll_coverage is not None:
        coverage = state.broll_coverage
    return BrollRules(
        min_start_s=b.min_start_s,
        min_duration_s=b.min_duration_s,
        max_duration_s=b.max_duration_s,
        max_coverage=coverage,
        min_gap_s=b.min_gap_s,
    )


def _placements(ctx: StepContext, *statuses: PlacementStatus) -> list[BrollPlacement]:
    stmt = select(BrollPlacement).where(BrollPlacement.version_id == ctx.version.id)
    if statuses:
        stmt = stmt.where(BrollPlacement.status.in_(statuses))
    return list(ctx.session.scalars(stmt.order_by(BrollPlacement.number)))


def step_broll_plan(ctx: StepContext) -> None:
    """6.3: LLM suggests spots, the rules decide. Later versions keep their (edited) spots."""
    if not ctx.settings.broll.enabled:
        ctx.metrics["skipped"] = "disabled"
        return
    if ctx.version.parent_id is not None:
        ctx.metrics.update(reused=True, placements=len(_placements(ctx)))
        return
    llm = ctx.providers.llm()
    if llm is None:
        ctx.metrics["skipped"] = "no_llm"
        return
    transcript = _transcript(ctx)
    timeline, _, segments, state = _structure(ctx, transcript)
    prompt = load_prompt(
        "broll_plan",
        max=str(planner.MAX_SUGGESTIONS),
        transcript=vocab.transcript_lines(segments, transcript.words, state.overlay),
    )
    used_before = len(llm.usage)
    try:
        response = llm.structured("broll_plan", prompt, BrollPlanResponse)
    except LLMError as exc:
        log.warning("broll_plan_failed", extra={"error": str(exc)})
        ctx.metrics["skipped"] = "llm_error"
        return
    spots = planner.validate(response.suggestions, timeline.kept_word_ids(transcript.words))
    chosen = planner.place(spots, transcript.words, timeline, _broll_rules(ctx.settings))
    ctx.session.execute(delete(BrollPlacement).where(BrollPlacement.version_id == ctx.version.id))
    for number, (spot, _) in enumerate(chosen, start=1):
        ctx.session.add(
            BrollPlacement(
                version_id=ctx.version.id,
                number=number,
                first_word=spot.first_word,
                last_word=spot.last_word,
                kind=spot.kind,
                idea=spot.idea,
                query=spot.query,
                card_text=spot.card_text,
                priority=spot.priority,
                status=PlacementStatus.PLANNED,
            )
        )
    # All valid suggestions, so a coverage change can pick more or fewer without a new LLM call.
    ctx.version.edits = {
        **ctx.version.edits,
        "broll_candidates": [s.model_dump(mode="json") for s in spots],
    }
    calls = llm.usage[used_before:]
    ctx.metrics.update(
        suggested=len(response.suggestions),
        valid=len(spots),
        placed=len(chosen),
        llm_input_tokens=sum(u.input_tokens for u in calls),
        llm_output_tokens=sum(u.output_tokens for u in calls),
    )


def _upsert_asset(
    ctx: StepContext, source: BrollSource, source_id: str, **fields: Any
) -> BrollAsset:
    asset = ctx.session.scalar(
        select(BrollAsset).where(BrollAsset.source == source, BrollAsset.source_id == source_id)
    )
    if asset is None:
        asset = BrollAsset(source=source, source_id=source_id, **fields)
        ctx.session.add(asset)
        ctx.session.flush()
    return asset


def _words_text(transcript: Transcript, overlay: dict[int, str], first: int, last: int) -> str:
    return " ".join(
        display_text(w, overlay) for w in transcript.words if first <= w.id <= last
    ).strip()


def step_broll_fetch(ctx: StepContext) -> None:
    """6.5-6.7: an asset for every planned spot: text card, or a checked stock clip."""
    if not ctx.settings.broll.enabled:
        ctx.metrics["skipped"] = "disabled"
        return
    rules = _broll_rules(ctx.settings)
    planned = _placements(ctx, PlacementStatus.PLANNED)
    transcript = _transcript(ctx)
    state = version_state(ctx.version)
    llm = ctx.providers.llm()
    stock = ctx.providers.stock()
    used_before = len(llm.usage) if llm else 0
    counts: Counter[str] = Counter()
    for placement in planned:
        if placement.kind == "textcard" and placement.card_text:
            video = textcards.render_textcard(
                placement.card_text,
                ctx.app.data_dir / "cache" / "textcards",
                rules.max_duration_s,
                textcards.template_path(ctx.app.templates_dir),
                ctx.app.fonts_dir,
                accent_color=ctx.settings.publish.cover_accent_color,
            )
            card = _upsert_asset(
                ctx, BrollSource.TEXTCARD, video.stem, media_type="video", path=ctx.rel(video),
                width=1080, height=1920, duration_s=rules.max_duration_s,
                description=placement.card_text,
            )  # fmt: skip
            placement.asset_id, placement.status = card.id, PlacementStatus.READY
            counts["textcard"] += 1
            continue
        context = _words_text(transcript, state.overlay, placement.first_word, placement.last_word)
        asset, score, reason = _find_stock(ctx, placement, context, rules, llm, stock)
        if asset is None:
            placement.status, placement.reason = PlacementStatus.SKIPPED, reason
            counts[f"skipped_{reason}"] += 1
        else:
            placement.asset_id, placement.score = asset.id, score
            placement.status = PlacementStatus.READY
            counts["stock"] += 1
    calls = llm.usage[used_before:] if llm else []
    ctx.metrics.update(
        **counts,
        llm_input_tokens=sum(u.input_tokens for u in calls),
        llm_output_tokens=sum(u.output_tokens for u in calls),
    )


STOCK_HITS_CHECKED = 3  # few downloads per spot (Pixabay: no systematic mass downloads)


def _find_stock(
    ctx: StepContext,
    placement: BrollPlacement,
    context: str,
    rules: BrollRules,
    llm: LLMProvider | None,
    stock: PixabayClient | None,
) -> tuple[BrollAsset | None, float | None, str]:
    """The client's own library first, then stock: the first candidate whose preview passes
    the vision check; else (None, None, reason)."""
    if llm is None:
        return None, None, "no_check"
    own = _find_in_library(ctx, placement, context, rules, llm)
    if own is not None:
        return own[0], own[1], ""
    if stock is None:
        return None, None, "no_stock_key"
    try:
        hits = stock.search(placement.query or placement.idea, rules.max_duration_s)
    except StockUnavailableError as exc:
        log.warning("stock_unavailable", extra={"error": str(exc)})
        return None, None, "stock_unavailable"
    for hit in hits[:STOCK_HITS_CHECKED]:
        try:
            thumb = stock.download_thumbnail(hit)
            if thumb is None:
                continue
            score = check_image(llm, thumb, placement.idea, context)
            if score < ctx.settings.broll.min_score:
                continue
            video = stock.download_video(hit)
        except StockUnavailableError as exc:
            log.warning("stock_download_failed", extra={"error": str(exc)})
            continue
        asset = _upsert_asset(
            ctx, BrollSource.PIXABAY, str(hit.id), source_url=hit.page_url, media_type="video",
            path=ctx.rel(video), thumbnail=ctx.rel(thumb), width=hit.width, height=hit.height,
            duration_s=hit.duration, description=hit.tags, meta=PixabayClient.meta(hit),
        )  # fmt: skip
        return asset, float(score), ""
    return None, None, "no_match"


LIBRARY_HITS_CHECKED = 3


def _find_in_library(
    ctx: StepContext,
    placement: BrollPlacement,
    context: str,
    rules: BrollRules,
    llm: LLMProvider,
) -> tuple[BrollAsset, float] | None:
    try:
        (vector,) = llm.embed([f"{placement.idea} {placement.query or ''}".strip()])
        hits = library.search(
            ctx.session, ctx.client.id, vector, rules.min_duration_s, LIBRARY_HITS_CHECKED
        )
    except Exception as exc:  # no embedding model etc.: fall back to stock
        log.warning("library_search_failed", extra={"error": str(exc)})
        return None
    for asset in hits:
        if asset.thumbnail is None:
            continue
        score = check_image(llm, ctx.path(asset.thumbnail), placement.idea, context)
        if score >= ctx.settings.broll.min_score:
            return asset, float(score)
    return None


def _overlays(ctx: StepContext) -> list[tuple[int, Overlay]]:
    """(B-number, overlay) for ready placements on the current output timeline, rules applied
    again (a correction may have cut words: their B-roll disappears)."""
    if not ctx.settings.broll.enabled:
        return []
    ready = [p for p in _placements(ctx, PlacementStatus.READY) if p.asset_id is not None]
    if not ready:
        return []
    transcript = _transcript(ctx)
    timeline = Timeline(_plan(ctx).keep)
    candidates = []
    for p in ready:
        span = word_span(transcript.words, p.first_word, p.last_word, timeline)
        if span is not None:
            candidates.append(Candidate(key=p.id, start=span[0], end=span[1], priority=p.priority))
    by_id = {p.id: p for p in ready}
    overlays = []
    rules = _broll_rules(ctx.settings, version_state(ctx.version))
    for slot in select_slots(candidates, timeline.duration, rules):
        placement = by_id[slot.key]
        asset = ctx.session.get(BrollAsset, placement.asset_id)
        if asset is None or asset.path is None:
            continue
        overlay = Overlay(
            ctx.path(asset.path), slot.start, slot.end, still=asset.media_type == "image"
        )
        overlays.append((placement.number, overlay))
    return overlays


def step_subtitles(ctx: StepContext) -> None:
    transcript = _transcript(ctx)
    timeline, spans, _, state = _structure(ctx, transcript)
    # Client settings, overridden by this version's edits (e.g. move_subtitles).
    subtitle = SubtitleSettings.model_validate(
        {**ctx.settings.subtitle.model_dump(mode="json"), **state.subtitle}
    )
    steps.subtitles_step(
        transcript,
        timeline,
        spans,
        state.overlay,
        [int(i) for i in ctx.version.highlights],
        ctx.settings.model_copy(update={"subtitle": subtitle}),
        ctx.paths.version(ctx.version.number).subtitles,
    )
    ctx.version.subtitle_style = subtitle.model_dump(mode="json")
    ctx.metrics.update(karaoke=subtitle.karaoke, position=subtitle.position)


def step_correction(ctx: StepContext) -> None:
    """Interpret the newest received correction of this version (LLM -> validated plan)."""
    correction = ctx.session.scalar(
        select(Correction)
        .where(
            Correction.version_id == ctx.version.id, Correction.status == CorrectionStatus.RECEIVED
        )
        .order_by(Correction.id.desc())
        .limit(1)
    )
    if correction is None:
        ctx.metrics["correction_id"] = None
        return
    ctx.metrics["correction_id"] = correction.id
    llm = ctx.providers.llm()
    if llm is None:
        correction.status = CorrectionStatus.REJECTED
        correction.plan = {"error": "llm_unavailable"}
        return

    transcript = _transcript(ctx)
    _, spans, segments, state = _structure(ctx, transcript)
    removed = removed_segments(spans, state.removed_words)
    by_id = {w.id: w for w in transcript.words}
    removed_lines = [
        f"E{n}: "
        + " ".join(
            display_text(by_id[i], state.overlay)
            for i in range(span.first_word, span.last_word + 1)
            if display_text(by_id[i], state.overlay).strip()
        )
        for n, span in enumerate(removed, start=1)
    ]
    shown = broll_lines(ctx.session, ctx.version)
    broll_text = "\n".join(f"B{n}: {p.card_text or p.idea}" for n, p in shown) or "keine"
    prompt = load_prompt(
        "corrections",
        version=str(ctx.version.number),
        broll=broll_text,
        segments="\n".join(f"{s.number}: {s.text}" for s in segments),
        removed="\n".join(removed_lines) or "keine",
        message=correction.message_text,
    )
    used_before = len(llm.usage)
    response = llm.structured("corrections", prompt, CorrectionResponse)
    resolved, issues = resolve(
        response.operations, transcript.words, segments, removed, state, [n for n, _ in shown]
    )
    understood = bool(resolved) and not issues and not response.clarification
    correction.plan = {
        "operations": [op.model_dump(mode="json") for op in response.operations],
        "resolved": [r.model_dump(mode="json") for r in resolved],
        "issues": [i.model_dump(mode="json") for i in issues],
        "clarification": response.clarification,
        "rerun_step": rerun_step([r.op for r in resolved], STEP_ORDER),
    }
    correction.status = (
        CorrectionStatus.PLANNED if understood else CorrectionStatus.NEEDS_CLARIFICATION
    )
    calls = llm.usage[used_before:]
    ctx.metrics.update(
        status=correction.status.value,
        operations=len(response.operations),
        issues=len(issues),
        llm_input_tokens=sum(u.input_tokens for u in calls),
        llm_output_tokens=sum(u.output_tokens for u in calls),
    )


# --- publish steps ----------------------------------------------------------------------


def _segments_text(ctx: StepContext, limit: int = 6000) -> tuple[str, str]:
    """Numbered segment texts of this version (capped) and the first sentence."""
    _, _, segments, _ = _structure(ctx, _transcript(ctx))
    text = "\n".join(f"{s.number}: {s.text}" for s in segments)[:limit]
    return text, segments[0].text if segments else ""


def _confirmed_terms(ctx: StepContext) -> list[GlossaryTerm]:
    return [t for t in load_terms(ctx.session, ctx.client.id) if t.status == "confirmed"]


def step_frames(ctx: StepContext) -> None:
    package = get_package(ctx.session, ctx.version.id)
    out = ctx.paths.version(ctx.version.number).dir / "frames"
    best = frames.collect(_original(ctx), _plan(ctx).keep, out, FRAME_SAMPLES, FRAME_CANDIDATES)
    package.candidates = [
        {
            "time": f.time,
            "path": ctx.rel(f.path),
            "small": ctx.rel(f.path.with_name(f.path.stem + "_small.jpg")),
            "sharpness": round(f.sharpness, 1),
            "faces": f.faces,
            "face_area": f.face_area,
        }
        for f in best
    ]
    ctx.metrics.update(candidates=len(best), with_faces=sum(1 for f in best if f.faces))


def step_cover_plan(ctx: StepContext) -> None:
    package = get_package(ctx.session, ctx.version.id)
    candidates = package.candidates
    if not candidates:
        raise PermanentStepError("no cover candidates", "no_frames")
    transcript_text, first_sentence = _segments_text(ctx)
    terms = _confirmed_terms(ctx)
    suggestions: list[tuple[int, str]] = []
    llm = ctx.providers.llm()
    if llm is not None:
        used_before = len(llm.usage)
        prompt = load_prompt(
            "covers",
            count=str(len(candidates)),
            terms=", ".join(t.term for t in terms) or "keine",
            caption=ctx.project.caption or "",
            transcript=transcript_text,
        )
        try:
            response = llm.structured(
                "covers",
                prompt,
                CoverPlanResponse,
                images=[ctx.path(c["small"]) for c in candidates],
            )
            suggestions = [(c.image, c.headline) for c in response.covers]
        except LLMError as exc:
            log.warning("cover_plan_llm_failed", extra={"error": str(exc)})
        calls = llm.usage[used_before:]
        ctx.metrics.update(
            llm_input_tokens=sum(u.input_tokens for u in calls),
            llm_output_tokens=sum(u.output_tokens for u in calls),
        )
    planned = cover_plan.plan_covers(suggestions, len(candidates), first_sentence)
    package.covers = [
        {
            "number": c.number,
            "frame": candidates[c.candidate]["path"],
            "headline": apply_glossary_text(c.headline, terms),
        }
        for c in planned
    ]
    ctx.metrics.update(covers=len(planned), from_llm=sum(1 for _ in suggestions))


def step_covers(ctx: StepContext) -> None:
    package = get_package(ctx.session, ctx.version.id)
    publish = ctx.settings.publish
    specs = [
        covers.CoverSpec(c["number"], ctx.path(c["frame"]), c["headline"]) for c in package.covers
    ]
    out_dir = ctx.paths.version(ctx.version.number).dir / "covers"
    pngs = covers.render_covers(
        specs,
        covers.template_path(ctx.app.templates_dir, publish.cover_template),
        ctx.app.fonts_dir,
        out_dir,
        publish.cover_text_color,
        publish.cover_accent_color,
    )
    package.covers = [
        {**c, "path": ctx.rel(png)} for c, png in zip(package.covers, pngs, strict=True)
    ]
    ctx.metrics.update(rendered=len(pngs), template=publish.cover_template)


def _store_post(package: PublishPackage, post: dict[str, Any]) -> None:
    package.post = post
    package.post_text = post_text.compose(post)


def step_post_text(ctx: StepContext) -> None:
    package = get_package(ctx.session, ctx.version.id)
    transcript_text, first_sentence = _segments_text(ctx)
    terms = _confirmed_terms(ctx)
    max_tags = ctx.settings.publish.max_hashtags
    llm = ctx.providers.llm()
    post: dict[str, Any] | None = None
    if llm is not None:
        used_before = len(llm.usage)
        prompt = load_prompt(
            "post",
            tone=post_text.tone_description(ctx.settings.tone),
            max_hashtags=str(max_tags),
            terms=", ".join(t.term for t in terms) or "keine",
            caption=ctx.project.caption or "",
            transcript=transcript_text,
        )
        try:
            draft = llm.structured("post_text", prompt, PostDraft)
            post = post_text.finalize(
                draft.hook, draft.body, draft.cta, draft.hashtags, terms, max_tags
            )
        except LLMError as exc:
            log.warning("post_text_llm_failed", extra={"error": str(exc)})
        calls = llm.usage[used_before:]
        ctx.metrics.update(
            llm_input_tokens=sum(u.input_tokens for u in calls),
            llm_output_tokens=sum(u.output_tokens for u in calls),
        )
    ctx.metrics["fallback"] = post is None
    _store_post(package, post or post_text.fallback_post(first_sentence, terms, max_tags))
    package.revision = 1
    package.status = PublishStatus.READY


def step_post_revise(ctx: StepContext) -> None:
    package = get_package(ctx.session, ctx.version.id)
    request = package.revision_request
    if not request:
        ctx.metrics["request"] = None
        return
    llm = ctx.providers.llm()
    if llm is None:
        raise PermanentStepError("no LLM for text changes", "llm_unavailable")
    terms = _confirmed_terms(ctx)
    max_tags = ctx.settings.publish.max_hashtags
    current = package.post
    prompt = load_prompt(
        "post_revise",
        tone=post_text.tone_description(ctx.settings.tone),
        max_hashtags=str(max_tags),
        terms=", ".join(t.term for t in terms) or "keine",
        hook=str(current.get("hook", "")),
        body=str(current.get("body", "")),
        cta=str(current.get("cta", "")),
        hashtags=" ".join(current.get("hashtags", [])),
        request=request,
    )
    used_before = len(llm.usage)
    draft = llm.structured("post_revise", prompt, PostDraft)
    post = post_text.finalize(draft.hook, draft.body, draft.cta, draft.hashtags, terms, max_tags)
    _store_post(package, {**post, "revision_failed": False})
    package.revision += 1
    package.revision_request = None
    calls = llm.usage[used_before:]
    ctx.metrics.update(
        revision=package.revision,
        llm_input_tokens=sum(u.input_tokens for u in calls),
        llm_output_tokens=sum(u.output_tokens for u in calls),
    )


def broll_lines(session: Session, version: Version) -> list[tuple[int, BrollPlacement]]:
    """Placements shown to the client for this version (the ones rendered, if known)."""
    rows = session.scalars(
        select(BrollPlacement)
        .where(
            BrollPlacement.version_id == version.id,
            BrollPlacement.status == PlacementStatus.READY,
        )
        .order_by(BrollPlacement.number)
    ).all()
    rendered = version.edits.get("broll_rendered")
    return [(p.number, p) for p in rows if rendered is None or p.number in rendered]


def apply_broll_edits(session: Session, version: Version, resolved: Sequence[Resolved]) -> None:
    """remove/replace/add_broll and set_broll_coverage on the placements of a new version."""
    placements = {
        p.number: p
        for p in session.scalars(
            select(BrollPlacement).where(BrollPlacement.version_id == version.id)
        )
    }
    next_number = max(placements, default=0) + 1
    for r in resolved:
        op = r.op
        if isinstance(op, RemoveBroll) and op.broll in placements:
            session.delete(placements.pop(op.broll))
        elif isinstance(op, ReplaceBroll) and op.broll in placements:
            p = placements[op.broll]
            p.idea, p.card_text = op.idea, op.card_text
            p.kind = "textcard" if op.card_text else "stock"
            p.query = None if op.card_text else (op.query.strip() or op.idea)
            p.asset_id, p.score, p.reason = None, None, None
            p.status = PlacementStatus.PLANNED
        elif isinstance(op, AddBroll) and r.word_ids:
            placements[next_number] = BrollPlacement(
                version_id=version.id,
                number=next_number,
                first_word=r.word_ids[0],
                last_word=r.word_ids[-1],
                kind="textcard" if op.card_text else "stock",
                idea=op.idea,
                query=None if op.card_text else (op.query.strip() or op.idea),
                card_text=op.card_text,
                priority=1,  # asked for by the client
                status=PlacementStatus.PLANNED,
            )
            session.add(placements[next_number])
            next_number += 1
        elif isinstance(op, SetBrollCoverage) and op.coverage > 0:
            # More room: offer the planner's other suggestions; the rules pick at render time.
            taken = {(p.first_word, p.last_word) for p in placements.values()}
            for c in version.edits.get("broll_candidates", []):
                if (c["first_word"], c["last_word"]) in taken:
                    continue
                placements[next_number] = BrollPlacement(
                    version_id=version.id, number=next_number, first_word=c["first_word"],
                    last_word=c["last_word"], kind=c["kind"], idea=c["idea"], query=c["query"],
                    card_text=c["card_text"], priority=c["priority"],
                    status=PlacementStatus.PLANNED,
                )  # fmt: skip
                session.add(placements[next_number])
                taken.add((c["first_word"], c["last_word"]))
                next_number += 1
    version.edits = {k: v for k, v in version.edits.items() if k != "broll_rendered"}
    session.flush()


def create_next_version(
    session: Session, base: Version, state: VersionState, from_step: str
) -> Version:
    """Version n+1 from a confirmed correction; rendered again from `from_step` on."""
    number = (
        session.scalar(
            select(func.max(Version.number)).where(Version.project_id == base.project_id)
        )
        or 0
    ) + 1
    version = Version(
        project_id=base.project_id,
        number=number,
        parent_id=base.id,
        transcript_id=base.transcript_id,
        status=VersionStatus.PENDING,
        cut_plan=base.cut_plan,
        text_overlay={str(k): v for k, v in sorted(state.overlay.items())},
        highlights=sorted(state.highlights),
        subtitle_style=base.subtitle_style,
        edits=state.edits(),
    )
    session.add(version)
    session.flush()
    for p in session.scalars(select(BrollPlacement).where(BrollPlacement.version_id == base.id)):
        session.add(
            BrollPlacement(
                version_id=version.id, number=p.number, first_word=p.first_word,
                last_word=p.last_word, kind=p.kind, idea=p.idea, query=p.query,
                card_text=p.card_text, priority=p.priority, asset_id=p.asset_id, score=p.score,
                status=p.status, reason=p.reason,
            )
        )  # fmt: skip
    queue.enqueue(session, version.id, from_step)
    project = session.get(Project, base.project_id)
    assert project is not None
    project.status = ProjectStatus.PROCESSING
    return version


def step_render(ctx: StepContext) -> None:
    vpaths = ctx.paths.version(ctx.version.number)
    numbered = _overlays(ctx)
    overlays = [o for _, o in numbered]
    # The preview lists exactly the B-roll that is in the video.
    ctx.version.edits = {**ctx.version.edits, "broll_rendered": [n for n, _ in numbered]}
    export = steps.render_step(
        _original(ctx),
        _probe(ctx),
        _plan(ctx),
        vpaths.subtitles,
        ctx.settings,
        vpaths.export,
        ctx.app.fonts_dir,
        ctx.app.render_preset,
        overlays,
    )
    ctx.metrics["broll"] = len(overlays)
    rel = ctx.rel(export)
    ctx.session.execute(
        delete(Asset).where(Asset.version_id == ctx.version.id, Asset.kind == AssetKind.EXPORT)
    )
    size = export.stat().st_size
    ctx.session.add(
        Asset(
            project_id=ctx.project.id,
            version_id=ctx.version.id,
            kind=AssetKind.EXPORT,
            path=rel,
            size_bytes=size,
        )
    )
    ctx.version.export_path = rel
    ctx.version.status = VersionStatus.READY
    ctx.project.status = ProjectStatus.REVIEW
    ctx.metrics.update(export=rel, size_bytes=size)


STEPS: dict[str, Callable[[StepContext], None]] = {
    "probe": step_probe,
    "audio": step_audio,
    "transcribe": step_transcribe,
    "glossary": step_glossary,
    "vocab": step_vocab,
    "vad": step_vad,
    "cutplan": step_cutplan,
    "segments": step_segments,
    "highlights": step_highlights,
    "broll_plan": step_broll_plan,
    "broll_fetch": step_broll_fetch,
    "subtitles": step_subtitles,
    "render": step_render,
}
assert tuple(STEPS) == STEP_ORDER
ALL_STEPS: dict[str, Callable[[StepContext], None]] = {
    **STEPS,
    CORRECTION_STEP: step_correction,
    "frames": step_frames,
    "cover_plan": step_cover_plan,
    "covers": step_covers,
    "post_text": step_post_text,
    POST_REVISE_STEP: step_post_revise,
}
assert all(step in ALL_STEPS for step in PUBLISH_ORDER)
