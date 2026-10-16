"""Sequential pipeline for version 1 (Etappe 1: CLI, no queue). No database access."""

from __future__ import annotations

import time
from collections import Counter
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path

from reels.domain.glossary import GlossaryTerm, build_prompt_hint
from reels.domain.timeline import Timeline
from reels.llm.base import LLMProvider, LLMUsage
from reels.log import get_logger
from reels.pipeline import steps
from reels.pipeline.steps import ProjectPaths
from reels.settings_schema import ClientSettings
from reels.transcribe.base import TranscriptionEngine
from reels.vad.silero import VoiceActivityDetector

log = get_logger(__name__)


class NoSpeechError(RuntimeError):
    pass


@dataclass
class ProcessResult:
    version_dir: Path
    export: Path
    original_duration: float
    output_duration: float
    cut_count: int
    removed_fillers: int
    words: int
    segments: int
    highlights: int
    glossary_hits: Counter[str]
    timings: dict[str, float] = field(default_factory=dict)
    llm_usage: list[LLMUsage] = field(default_factory=list)


class _Timer:
    def __init__(self) -> None:
        self.timings: dict[str, float] = {}

    @contextmanager
    def step(self, name: str) -> Iterator[None]:
        start = time.perf_counter()
        try:
            yield
        finally:
            elapsed = time.perf_counter() - start
            self.timings[name] = elapsed
            log.info("step_done", extra={"step": name, "duration_s": round(elapsed, 3)})


def process_v1(
    source: Path,
    paths: ProjectPaths,
    settings: ClientSettings,
    terms: Sequence[GlossaryTerm],
    engine: TranscriptionEngine,
    vad: VoiceActivityDetector,
    llm: LLMProvider | None,
    fonts_dir: Path,
    render_preset: str | None = None,
) -> ProcessResult:
    timer = _Timer()
    version = paths.version(1)

    with timer.step("store_original"):
        original = steps.store_original(source, paths)
    with timer.step("probe"):
        src = steps.probe_step(original, paths, settings)
    with timer.step("audio"):
        steps.audio_step(original, paths)
    with timer.step("transcribe"):
        transcript = steps.transcribe_step(paths, engine, build_prompt_hint(terms))
    with timer.step("glossary"):
        overlay, hits = steps.glossary_step(transcript, terms)
        steps.write_json(version.overlay, {str(k): v for k, v in sorted(overlay.items())})
    with timer.step("vad"):
        speech = steps.vad_step(paths, vad)
    with timer.step("cutplan"):
        plan = steps.cutplan_step(transcript, speech, src, settings)
        if not plan.keep:
            raise NoSpeechError("no speech found in the video")
        steps.write_json(version.cutplan, plan)
        timeline = Timeline(plan.keep)
    with timer.step("segments"):
        spans, segments = steps.segments_step(transcript, timeline, overlay)
        steps.write_json(version.segments, segments)
    with timer.step("highlights"):
        usage_before = len(llm.usage) if llm else 0
        highlights = steps.highlights_step(llm, transcript, segments, timeline, overlay)
        steps.write_json(version.highlights, highlights)
    with timer.step("subtitles"):
        ass = steps.subtitles_step(
            transcript, timeline, spans, overlay, highlights, settings, version.subtitles
        )
    with timer.step("render"):
        export = steps.render_step(
            original, src, plan, ass, settings, version.export, fonts_dir, render_preset
        )

    return ProcessResult(
        version_dir=version.dir,
        export=export,
        original_duration=src.duration,
        output_duration=plan.output_duration,
        cut_count=plan.cut_count,
        removed_fillers=int(plan.params["removed_words"]),
        words=len(transcript.words),
        segments=len(segments),
        highlights=len(highlights),
        glossary_hits=hits,
        timings=timer.timings,
        llm_usage=list(llm.usage[usage_before:]) if llm else [],
    )
