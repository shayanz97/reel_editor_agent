"""WhisperX on CPU (int8) with wav2vec2 word alignment."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from reels.domain.words import RawWord, Transcript
from reels.transcribe.base import make_transcript


class WhisperXEngine:
    name = "whisperx"

    def __init__(self, model: str, batch_size: int = 8, device: str = "cpu") -> None:
        self.model = model
        self.batch_size = batch_size
        self.device = device

    def transcribe(self, audio: Path, language: str = "de", hint: str | None = None) -> Transcript:
        import whisperx  # heavy import (torch), only when actually used

        samples = whisperx.load_audio(str(audio))
        asr = whisperx.load_model(
            self.model,
            self.device,
            compute_type="int8",
            language=language,
            # Silero instead of the default pyannote VAD: no extra model access needed.
            vad_method="silero",
            asr_options={"initial_prompt": hint} if hint else None,
        )
        result = asr.transcribe(samples, batch_size=self.batch_size, language=language)
        align_model, metadata = whisperx.load_align_model(
            language_code=language, device=self.device
        )
        aligned = whisperx.align(
            result["segments"], align_model, metadata, samples, self.device,
            return_char_alignments=False,
        )  # fmt: skip
        return make_transcript(self.name, self.model, language, _raw_words(aligned))


def _raw_words(aligned: dict[str, Any]) -> list[RawWord]:
    # Words without timing (e.g. digits the aligner cannot place) are kept and interpolated.
    return [
        RawWord(text=w["word"], start=w.get("start"), end=w.get("end"), score=w.get("score"))
        for w in aligned.get("word_segments", [])
    ]
