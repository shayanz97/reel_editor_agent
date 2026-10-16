"""Fake transcript/VAD that match the synthetic test video exactly."""

from reels.domain.words import RawWord
from reels.transcribe.base import FakeEngine
from reels.vad.silero import FixedVad
from tests.fixtures.synthetic_video import VideoSpec

WORD_S = 0.2


def fake_words(spec: VideoSpec) -> list[RawWord]:
    """Each tone becomes one sentence of 0.2 s words that fill it exactly."""
    raw: list[RawWord] = []
    n = 0
    for tone in (i for i in spec.intervals if i.kind == "tone"):
        count = max(1, round((tone.end - tone.start) / WORD_S))
        step = (tone.end - tone.start) / count
        for k in range(count):
            text = f"wort{n}" + ("." if k == count - 1 else "")
            raw.append(
                RawWord(text=text, start=tone.start + k * step, end=tone.start + (k + 1) * step)
            )
            n += 1
    return raw


def fakes(spec: VideoSpec) -> tuple[FakeEngine, FixedVad]:
    tones = [(i.start, i.end) for i in spec.intervals if i.kind == "tone"]
    return FakeEngine(fake_words(spec)), FixedVad(tones)
