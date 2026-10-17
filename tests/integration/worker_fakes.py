"""Providers for worker tests. Importable from spawned processes, configured via env vars.

REELS_TEST_SPEC     synthetic video JSON sidecar -> fake words and VAD regions
REELS_TEST_ENGINE   ok | fail | sleep-first:<s> (sleep only on the first transcribe call)
REELS_TEST_CALLS    file that gets one line per transcribe call (counts across processes)
REELS_TEST_NOTIFY   file that gets one line per admin notification
REELS_TEST_LLM      1 -> fake LLM that highlights the first word of each sentence
"""

from __future__ import annotations

import json
import os
import time
from pathlib import Path
from typing import Any

from reels.domain.words import Transcript
from reels.llm.base import LLMUsage
from reels.llm.schemas import HighlightPlan, SegmentHighlight
from reels.pipeline.runner import Providers
from reels.transcribe.base import FakeEngine
from reels.vad.silero import FixedVad
from tests.fixtures.synthetic_video import Interval, VideoSpec
from tests.integration.pipeline_helpers import fake_words


def load_spec(path: str) -> VideoSpec:
    data = json.loads(Path(path).read_text())
    data["intervals"] = [Interval(**i) for i in data["intervals"]]
    return VideoSpec(**data)


class ControlledEngine(FakeEngine):
    def __init__(self, spec: VideoSpec, mode: str, calls_file: str | None) -> None:
        super().__init__(fake_words(spec))
        self.mode = mode
        self.calls_file = calls_file

    def transcribe(self, audio: Path, language: str = "de", hint: str | None = None) -> Transcript:
        previous = 0
        if self.calls_file:
            path = Path(self.calls_file)
            previous = len(path.read_text().splitlines()) if path.exists() else 0
            with path.open("a") as fh:
                fh.write(f"{os.getpid()}\n")
        if self.mode == "fail":
            raise RuntimeError("engine exploded (test)")
        if self.mode.startswith("sleep-first:") and previous == 0:
            time.sleep(float(self.mode.split(":", 1)[1]))
        return super().transcribe(audio, language, hint)


class FileNotifier:
    def __init__(self, path: str | None) -> None:
        self.path = path

    def notify_admin(self, text: str) -> None:
        if self.path:
            with open(self.path, "a") as fh:
                fh.write(text.replace("\n", " ") + "\n")


class FakeLLM:
    def __init__(self) -> None:
        self.usage: list[LLMUsage] = []

    def structured(self, task: str, prompt: str, schema: type[Any], images: Any = None) -> Any:
        """Marks the first word of every sentence as structure (lines `N: [id]word …`)."""
        self.usage.append(LLMUsage(task, "fake-model", 120, 30))
        items = []
        for line in prompt.splitlines():
            number, sep, rest = line.partition(": [")
            if sep and number.isdigit():
                first = int(rest.split("]", 1)[0])
                items.append(
                    SegmentHighlight(
                        segment=int(number),
                        kind="structure",
                        first_word_id=first,
                        last_word_id=first,
                        reason="t",
                    )
                )
        return HighlightPlan(segments=items)

    def embed(self, texts: list[str]) -> list[list[float]]:
        return [[0.0] for _ in texts]


def make_providers() -> Providers:
    spec = load_spec(os.environ["REELS_TEST_SPEC"])
    engine = ControlledEngine(
        spec, os.environ.get("REELS_TEST_ENGINE", "ok"), os.environ.get("REELS_TEST_CALLS")
    )
    vad = FixedVad([(i.start, i.end) for i in spec.intervals if i.kind == "tone"])
    llm = FakeLLM() if os.environ.get("REELS_TEST_LLM") == "1" else None
    return Providers(
        engine=lambda _name: engine,
        vad=lambda: vad,
        llm=lambda: llm,
        notifier=FileNotifier(os.environ.get("REELS_TEST_NOTIFY")),
    )
