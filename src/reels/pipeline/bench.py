"""Transcription benchmark: same audio, several engines, measured (never estimated)."""

from __future__ import annotations

import platform
import time
import wave
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

from reels.domain.word_compare import Comparison, DiffStats, compare_words
from reels.domain.words import Transcript
from reels.transcribe.base import TranscriptionEngine

MAX_DIFFERENCES_SHOWN = 40


@dataclass
class EngineRun:
    engine: str
    model: str
    seconds: list[float]
    transcript: Transcript


@dataclass
class BenchResult:
    source: str
    audio_seconds: float
    runs: list[EngineRun]
    comparison: Comparison | None
    machine: str = field(default_factory=lambda: f"{platform.platform()} ({platform.machine()})")
    created: str = field(default_factory=lambda: datetime.now(UTC).isoformat(timespec="seconds"))


def wav_duration(path: Path) -> float:
    with wave.open(str(path), "rb") as wf:
        return wf.getnframes() / wf.getframerate()


def run_benchmark(
    source: str,
    audio: Path,
    engines: Mapping[str, TranscriptionEngine],
    runs: int = 1,
    hint: str | None = None,
) -> BenchResult:
    results: list[EngineRun] = []
    for name, engine in engines.items():
        seconds: list[float] = []
        transcript: Transcript | None = None
        for _ in range(runs):
            start = time.perf_counter()
            transcript = engine.transcribe(audio, language="de", hint=hint)
            seconds.append(time.perf_counter() - start)
        assert transcript is not None
        results.append(EngineRun(name, engine.model, seconds, transcript))
    comparison = (
        compare_words(results[0].transcript.words, results[1].transcript.words)
        if len(results) >= 2
        else None
    )
    return BenchResult(source, wav_duration(audio), results, comparison)


def _fmt_stats(s: DiffStats | None) -> str:
    if s is None:
        return "–"
    return f"{s.mean:.3f} / {s.median:.3f} / {s.p90:.3f} / {s.max:.3f}"


def format_report(r: BenchResult) -> str:
    lines = [
        f"## Benchmark Transkription – {r.source}",
        "",
        f"- Datum: {r.created}",
        f"- Rechner: {r.machine}",
        f"- Audiolänge: {r.audio_seconds:.1f} s",
        "- Laufzeiten enthalten das Laden der Modelle (erster Lauf) – ab Lauf 2 ohne Download.",
        "",
        "| Engine | Modell | Laufzeiten (s) | Echtzeitfaktor (bester Lauf) | Wörter |",
        "| --- | --- | --- | --- | --- |",
    ]
    for run in r.runs:
        best = min(run.seconds)
        times = ", ".join(f"{s:.1f}" for s in run.seconds)
        rtf = best / r.audio_seconds if r.audio_seconds else 0.0
        lines.append(
            f"| {run.engine} | {run.model} | {times} | {rtf:.2f} | {len(run.transcript.words)} |"
        )
    c = r.comparison
    if c is not None:
        a, b = r.runs[0].engine, r.runs[1].engine
        lines += [
            "",
            f"### Vergleich {a} ↔ {b}",
            "",
            f"- Textübereinstimmung (normalisierte Wörter): {c.text_similarity:.1%}",
            f"- Zugeordnete Wörter: {c.matched} von {c.words_a} ({a}) / {c.words_b} ({b})",
            f"- Abweichung Wortanfang (s, Mittel/Median/P90/Max): {_fmt_stats(c.start_diff_s)}",
            f"- Abweichung Wortende (s, Mittel/Median/P90/Max): {_fmt_stats(c.end_diff_s)}",
            "",
            f"#### Textunterschiede ({len(c.differences)})",
            "",
            f"| {a} | {b} |",
            "| --- | --- |",
        ]
        for d in c.differences[:MAX_DIFFERENCES_SHOWN]:
            lines.append(f"| {d.a or '∅'} | {d.b or '∅'} |")
        if len(c.differences) > MAX_DIFFERENCES_SHOWN:
            lines.append(f"| … {len(c.differences) - MAX_DIFFERENCES_SHOWN} weitere | |")
    lines += ["", "### Volltexte", ""]
    for run in r.runs:
        lines += [f"**{run.engine}:**", "", run.transcript.text, ""]
    return "\n".join(lines)
