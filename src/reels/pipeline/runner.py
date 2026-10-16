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
from reels.domain.cutplan import CutPlan
from reels.domain.glossary import build_prompt_hint
from reels.domain.timeline import Timeline
from reels.domain.words import Transcript
from reels.llm.base import LLMProvider
from reels.log import get_logger, log_context
from reels.media.ffmpeg import FFmpegError
from reels.media.probe import ProbeResult, VideoRejectedError
from reels.pipeline import steps
from reels.pipeline.steps import ProjectPaths
from reels.settings_schema import ClientSettings
from reels.transcribe.base import TranscriptionEngine
from reels.vad.silero import VoiceActivityDetector

log = get_logger(__name__)

STEP_ORDER: tuple[str, ...] = (
    "probe",
    "audio",
    "transcribe",
    "glossary",
    "vad",
    "cutplan",
    "segments",
    "highlights",
    "subtitles",
    "render",
)
CLEANUP_STEP = "cleanup"


class PermanentStepError(Exception):
    """Retrying cannot help (video too long, no speech). The job fails immediately."""


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
                    STEPS[step](ctx)
                    metrics = ctx.metrics
                metrics["runtime_s"] = round(time.perf_counter() - start, 3)
                metrics["attempt"] = job.attempts
                queue.complete(session, job, metrics)
                following = next_step(step) if version_id is not None else None
                if following is not None:
                    queue.enqueue(session, version_id, following)
                session.commit()
            except Exception as exc:
                session.rollback()
                runtime = round(time.perf_counter() - start, 3)
                log.exception("step_failed", extra={"step": step})
                return handle_failure(
                    engine,
                    job_id,
                    _describe(exc),
                    providers.notifier,
                    permanent=isinstance(exc, PermanentStepError),
                    max_attempts=max_attempts,
                    metrics={"runtime_s": runtime},
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
        raise PermanentStepError("project has no original video")
    return ctx.path(rel)


def _probe(ctx: StepContext) -> ProbeResult:
    return ProbeResult.model_validate_json(ctx.paths.probe.read_text(encoding="utf-8"))


def _transcript(ctx: StepContext) -> Transcript:
    if ctx.version.transcript_id is None:
        raise RuntimeError("version has no transcript (transcribe step missing)")
    row = ctx.session.get(TranscriptRow, ctx.version.transcript_id)
    assert row is not None
    return Transcript(engine=row.engine, model=row.model, language=row.language, words=row.words)


def _overlay(ctx: StepContext) -> dict[int, str]:
    return {int(k): v for k, v in ctx.version.text_overlay.items()}


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
        raise PermanentStepError(str(exc)) from exc
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


def step_vad(ctx: StepContext) -> None:
    before = _mtime(ctx.paths.vad)
    regions = steps.vad_step(ctx.paths, ctx.providers.vad())
    ctx.metrics.update(
        reused=before is not None and before == _mtime(ctx.paths.vad), regions=len(regions)
    )


def step_cutplan(ctx: StepContext) -> None:
    speech = steps.load_speech(ctx.paths)
    src = _probe(ctx)
    plan = steps.cutplan_step(_transcript(ctx), speech, src, ctx.settings)
    if not plan.keep:
        raise PermanentStepError("no speech found in the video")
    ctx.version.cut_plan = plan.model_dump(mode="json")
    steps.write_json(ctx.paths.version(ctx.version.number).cutplan, plan)
    ctx.metrics.update(
        original_s=src.duration,
        output_s=round(plan.output_duration, 3),
        cuts=plan.cut_count,
        removed_words=int(plan.params["removed_words"]),
    )


def step_segments(ctx: StepContext) -> None:
    timeline = Timeline(_plan(ctx).keep)
    _, segments = steps.segments_step(_transcript(ctx), timeline, _overlay(ctx))
    steps.write_json(ctx.paths.version(ctx.version.number).segments, segments)
    ctx.metrics["segments"] = len(segments)


def step_highlights(ctx: StepContext) -> None:
    transcript, overlay = _transcript(ctx), _overlay(ctx)
    timeline = Timeline(_plan(ctx).keep)
    _, segments = steps.segments_step(transcript, timeline, overlay)
    llm = ctx.providers.llm()
    used_before = len(llm.usage) if llm else 0
    highlights = steps.highlights_step(llm, transcript, segments, timeline, overlay)
    ctx.version.highlights = highlights
    calls = llm.usage[used_before:] if llm else []
    ctx.metrics.update(
        highlights=len(highlights),
        llm_used=llm is not None,
        llm_calls=[
            {"task": u.task, "model": u.model, "in": u.input_tokens, "out": u.output_tokens}
            for u in calls
        ],
        llm_input_tokens=sum(u.input_tokens for u in calls),
        llm_output_tokens=sum(u.output_tokens for u in calls),
    )


def step_subtitles(ctx: StepContext) -> None:
    transcript, overlay = _transcript(ctx), _overlay(ctx)
    timeline = Timeline(_plan(ctx).keep)
    spans, _ = steps.segments_step(transcript, timeline, overlay)
    steps.subtitles_step(
        transcript,
        timeline,
        spans,
        overlay,
        [int(i) for i in ctx.version.highlights],
        ctx.settings,
        ctx.paths.version(ctx.version.number).subtitles,
    )
    ctx.version.subtitle_style = ctx.settings.subtitle.model_dump(mode="json")
    ctx.metrics["karaoke"] = ctx.settings.subtitle.karaoke


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
    "vad": step_vad,
    "cutplan": step_cutplan,
    "segments": step_segments,
    "highlights": step_highlights,
    "subtitles": step_subtitles,
    "render": step_render,
}
assert tuple(STEPS) == STEP_ORDER
