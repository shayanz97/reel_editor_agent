"""mlx-whisper on the Apple GPU, with Whisper's own word timestamps."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from reels.domain.words import RawWord, Transcript
from reels.transcribe.base import make_transcript


class MlxEngine:
    name = "mlx"

    def __init__(self, model: str) -> None:
        self.model = model

    def transcribe(self, audio: Path, language: str = "de", hint: str | None = None) -> Transcript:
        try:
            import mlx_whisper
        except ImportError as exc:  # only installed with `uv sync --extra mac`
            raise RuntimeError("mlx-whisper not installed; run `uv sync --extra mac`") from exc

        result = mlx_whisper.transcribe(
            str(audio),
            path_or_hf_repo=self.model,
            language=language,
            word_timestamps=True,
            initial_prompt=hint,
        )
        return make_transcript(self.name, self.model, language, _raw_words(result))


def _raw_words(result: dict[str, Any]) -> list[RawWord]:
    return [
        RawWord(text=w["word"], start=w.get("start"), end=w.get("end"), score=w.get("probability"))
        for seg in result.get("segments", [])
        for w in seg.get("words", [])
    ]
