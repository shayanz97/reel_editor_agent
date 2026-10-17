"""Pipeline as jobs: one job per step, chained in a fixed order.

A job's step work, its completion and the enqueueing of the next step are committed in ONE
transaction while the job row is locked. A crash therefore never leaves half a result behind,
and a restarted worker simply runs the step again (every step is idempotent).
"""

from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol

from sqlalchemy import Engine, delete, func, select
from sqlalchemy.orm import Session

from reels.config import Settings, get_settings
from reels.db import queue
from reels.db.glossary import add_hits, load_terms
from reels.db.models import (
    Asset,
    AssetKind,
    Client,
    Correction,
    CorrectionStatus,
    Job,
    JobStatus,
    Project,
    ProjectStatus,
    Version,
    VersionStatus,
)
from reels.db.models import (
    Transcript as TranscriptRow,
)
from reels.domain import vocab
from reels.domain.corrections import VersionState, removed_segments, rerun_step, resolve
from reels.domain.cutplan import CutPlan
from reels.domain.glossary import build_prompt_hint
from reels.domain.segments import Segment, Span, number_segments, split_sentences
from reels.domain.timeline import Timeline
from reels.domain.words import Transcript, display_text
from reels.llm.base import LLMError, LLMProvider, load_prompt
from reels.llm.schemas import CorrectionResponse, VocabResponse
from reels.log import get_logger, log_context
from reels.media.ffmpeg import FFmpegError
from reels.media.probe import ProbeResult, VideoRejectedError
from reels.pipeline import steps
from reels.pipeline.steps import ProjectPaths
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
    "subtitles",
    "render",
)
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


def default_providers() -> Providers:
    from reels.llm.openai_provider import OpenAIProvider
    from reels.transcribe.base import get_engine
    from reels.vad.silero import SileroVad

    return Providers(
        engine=get_engine,
        vad=SileroVad,
        llm=lambda: OpenAIProvider.from_settings(get_settings()),
    )


def next_step(step: str) -> str | None:
    if step not in STEP_ORDER:
        return None
    i = STEP_ORDER.index(step)
    return STEP_ORDER[i + 1] if i + 1 < len(STEP_ORDER) else None


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
    """Run a version again from `step` on (earlier results are reused). Caller commits."""
    if step not in STEP_ORDER:
        raise ValueError(f"unknown step {step!r}, expected one of {', '.join(STEP_ORDER)}")
    version = session.get(Version, version_id)
    if version is None:
        raise ValueError(f"version {version_id} not found")
    version.status = VersionStatus.PENDING
    return queue.enqueue(session, version_id, step)


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
    prompt = load_prompt(
        "corrections",
        version=str(ctx.version.number),
        segments="\n".join(f"{s.number}: {s.text}" for s in segments),
        removed="\n".join(removed_lines) or "keine",
        message=correction.message_text,
    )
    used_before = len(llm.usage)
    response = llm.structured("corrections", prompt, CorrectionResponse)
    resolved, issues = resolve(response.operations, transcript.words, segments, removed, state)
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
    queue.enqueue(session, version.id, from_step)
    project = session.get(Project, base.project_id)
    assert project is not None
    project.status = ProjectStatus.PROCESSING
    return version


def step_render(ctx: StepContext) -> None:
    vpaths = ctx.paths.version(ctx.version.number)
    export = steps.render_step(
        _original(ctx),
        _probe(ctx),
        _plan(ctx),
        vpaths.subtitles,
        ctx.settings,
        vpaths.export,
        ctx.app.fonts_dir,
        ctx.app.render_preset,
    )
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
    "subtitles": step_subtitles,
    "render": step_render,
}
assert tuple(STEPS) == STEP_ORDER
ALL_STEPS: dict[str, Callable[[StepContext], None]] = {**STEPS, CORRECTION_STEP: step_correction}
