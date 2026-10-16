"""Transcription engines: interchangeable, all return words with stable ids."""

from __future__ import annotations

from pathlib import Path
from typing import Protocol

from reels.domain.words import RawWord, Transcript, build_words


class TranscriptionEngine(Protocol):
    name: str
    model: str

    def transcribe(self, audio: Path, language: str = "de", hint: str | None = None) -> Transcript:
        """Transcribe a 16 kHz mono WAV. `hint` is the glossary prompt (may be ignored)."""
        ...


def make_transcript(engine: str, model: str, language: str, raw: list[RawWord]) -> Transcript:
    return Transcript(engine=engine, model=model, language=language, words=build_words(raw))


class FakeEngine:
    """Returns fixed words. Used in tests instead of the real engines."""

    name = "fake"
    model = "fake"

    def __init__(self, words: list[RawWord]) -> None:
        self.words = words
        self.calls: list[tuple[Path, str, str | None]] = []

    def transcribe(self, audio: Path, language: str = "de", hint: str | None = None) -> Transcript:
        self.calls.append((audio, language, hint))
        return make_transcript(self.name, self.model, language, self.words)


def get_engine(name: str) -> TranscriptionEngine:
    """Engine by name ("whisperx" or "mlx"); heavy libraries are imported lazily."""
    from reels.config import get_settings

    settings = get_settings()
    if name == "whisperx":
        from reels.transcribe.whisperx_engine import WhisperXEngine

        return WhisperXEngine(settings.whisperx_model, batch_size=settings.whisperx_batch_size)
    if name == "mlx":
        from reels.transcribe.mlx_engine import MlxEngine

        return MlxEngine(settings.mlx_whisper_model)
    raise ValueError(f"unknown transcription engine {name!r}")
