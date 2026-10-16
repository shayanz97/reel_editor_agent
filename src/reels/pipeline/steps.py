"""Pipeline steps: one function per step, explicit inputs and outputs.

Expensive steps are idempotent: existing results in the project folder are reused
(audio, transcript, VAD). Everything is always rendered from the original.
"""

from __future__ import annotations

import json
import shutil
from collections import Counter
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from pydantic import BaseModel, TypeAdapter

from reels.domain.cutplan import CutParams, CutPlan, build_cut_plan, filler_ids
from reels.domain.glossary import GlossaryTerm, apply_glossary
from reels.domain.highlights import select_highlights, transcript_for_prompt
from reels.domain.segments import Segment, Span, number_segments, split_sentences
from reels.domain.subtitles import GroupParams, build_events
from reels.domain.timeline import Timeline
from reels.domain.words import TextOverlay, Transcript
from reels.llm.base import LLMError, LLMProvider, load_prompt
from reels.llm.schemas import HighlightPlan
from reels.log import get_logger
from reels.media.ass import style_from_settings, write_ass
from reels.media.audio import extract_audio
from reels.media.probe import ProbeResult, check_processable, probe
from reels.media.render import RenderOptions, render
from reels.settings_schema import ClientSettings
from reels.transcribe.base import TranscriptionEngine
from reels.vad.silero import VoiceActivityDetector

log = get_logger(__name__)

_SPANS = TypeAdapter(list[tuple[float, float]])


@dataclass(frozen=True)
class VersionPaths:
    dir: Path

    @property
    def cutplan(self) -> Path:
        return self.dir / "cutplan.json"

    @property
    def overlay(self) -> Path:
        return self.dir / "overlay.json"

    @property
    def segments(self) -> Path:
        return self.dir / "segments.json"

    @property
    def highlights(self) -> Path:
        return self.dir / "highlights.json"

    @property
    def subtitles(self) -> Path:
        return self.dir / "subtitles.ass"

    @property
    def export(self) -> Path:
        return self.dir / "export.mp4"


@dataclass(frozen=True)
class ProjectPaths:
    root: Path

    @classmethod
    def for_project(cls, data_dir: Path, client_id: int, project_id: int) -> ProjectPaths:
        return cls(data_dir / "clients" / str(client_id) / "projects" / str(project_id))

    def original(self, suffix: str) -> Path:
        return self.root / f"original{suffix.lower()}"

    @property
    def audio(self) -> Path:
        return self.root / "audio.wav"

    @property
    def probe(self) -> Path:
        return self.root / "probe.json"

    @property
    def transcript(self) -> Path:
        return self.root / "transcript.json"

    @property
    def vad(self) -> Path:
        return self.root / "vad.json"

    def version(self, number: int) -> VersionPaths:
        return VersionPaths(self.root / f"v{number}")


def write_json(path: Path, data: BaseModel | Any) -> None:
    """Atomic JSON write (pydantic models or plain data)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    if isinstance(data, BaseModel):
        text = data.model_dump_json(indent=2)
    else:
        text = json.dumps(data, indent=2, ensure_ascii=False, default=_json_default)
    tmp.write_text(text + "\n", encoding="utf-8")
    tmp.replace(path)


def _json_default(obj: Any) -> Any:
    if isinstance(obj, BaseModel):
        return obj.model_dump(mode="json")
    raise TypeError(f"not JSON serializable: {type(obj).__name__}")


# --- steps -------------------------------------------------------------------


def store_original(source: Path, paths: ProjectPaths) -> Path:
    dst = paths.original(source.suffix or ".mp4")
    if dst.exists() and dst.stat().st_size == source.stat().st_size:
        return dst
    dst.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(source, dst)
    return dst


def probe_step(original: Path, paths: ProjectPaths, settings: ClientSettings) -> ProbeResult:
    result = probe(original)
    write_json(paths.probe, result)
    check_processable(result, settings.max_duration_s)
    return result


def audio_step(original: Path, paths: ProjectPaths) -> Path:
    return extract_audio(original, paths.audio)


def transcribe_step(
    paths: ProjectPaths, engine: TranscriptionEngine, hint: str | None, language: str = "de"
) -> Transcript:
    """Reuses transcript.json if it came from the same engine and model. Never edited later."""
    if paths.transcript.exists():
        cached = Transcript.model_validate_json(paths.transcript.read_text(encoding="utf-8"))
        if cached.engine == engine.name and cached.model == engine.model:
            log.info("transcript_reused", extra={"engine": engine.name})
            return cached
    transcript = engine.transcribe(paths.audio, language=language, hint=hint)
    write_json(paths.transcript, transcript)
    return transcript


def vad_step(paths: ProjectPaths, vad: VoiceActivityDetector) -> list[tuple[float, float]]:
    if paths.vad.exists():
        return _SPANS.validate_json(paths.vad.read_text(encoding="utf-8"))
    regions = vad.speech_regions(paths.audio)
    write_json(paths.vad, regions)
    return regions


def glossary_step(
    transcript: Transcript, terms: Sequence[GlossaryTerm]
) -> tuple[dict[int, str], Counter[str]]:
    return apply_glossary(transcript.words, terms)


def cutplan_step(
    transcript: Transcript,
    speech: Sequence[tuple[float, float]],
    src: ProbeResult,
    settings: ClientSettings,
    removed: Sequence[int] = (),
) -> CutPlan:
    params = CutParams(
        pause_threshold_s=settings.pause_threshold_s,
        pause_target_s=settings.pause_target_s,
        cut_padding_s=settings.cut_padding_s,
    )
    drop = set(removed)
    if settings.remove_fillers:
        drop |= filler_ids(transcript.words)
    return build_cut_plan(transcript.words, speech, src.duration, params, removed=drop)


def segments_step(
    transcript: Transcript, timeline: Timeline, overlay: TextOverlay
) -> tuple[list[Span], list[Segment]]:
    spans = split_sentences(transcript.words)
    kept = timeline.kept_word_ids(transcript.words)
    return spans, number_segments(transcript.words, spans, kept, overlay)


def highlights_step(
    llm: LLMProvider | None,
    transcript: Transcript,
    segments: Sequence[Segment],
    timeline: Timeline,
    overlay: TextOverlay,
) -> list[int]:
    """LLM picks highlight words; code validates them. Without LLM: no highlights."""
    if llm is None or not segments:
        return []
    prompt = load_prompt(
        "highlights", transcript=transcript_for_prompt(transcript.words, segments, overlay)
    )
    try:
        plan = llm.structured("highlights", prompt, HighlightPlan)
    except LLMError as exc:
        log.warning("highlights_failed", extra={"error": str(exc)})
        return []
    kept = timeline.kept_word_ids(transcript.words)
    visible = {w.id for w in transcript.words if overlay.get(w.id, w.text).strip()}
    return select_highlights([h.word_id for h in plan.highlights], segments, kept & visible)


def subtitles_step(
    transcript: Transcript,
    timeline: Timeline,
    spans: Sequence[Span],
    overlay: TextOverlay,
    highlights: Sequence[int],
    settings: ClientSettings,
    out: Path,
) -> Path:
    sub = settings.subtitle
    events = build_events(
        transcript.words,
        timeline,
        spans,
        GroupParams(max_words=sub.max_words, max_chars=sub.max_chars),
        overlay,
        set(highlights),
    )
    return write_ass(out, events, style_from_settings(sub), karaoke=sub.karaoke)


def render_step(
    original: Path,
    src: ProbeResult,
    plan: CutPlan,
    ass: Path,
    settings: ClientSettings,
    out: Path,
    fonts_dir: Path,
    preset: str | None = None,
) -> Path:
    opts = RenderOptions.from_settings(settings)
    if preset:
        opts = RenderOptions(**{**opts.__dict__, "preset": preset})
    return render(original, out, plan.keep, src, opts, ass, fonts_dir)
