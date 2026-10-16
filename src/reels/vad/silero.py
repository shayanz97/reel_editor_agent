"""Speech regions with Silero VAD."""

from __future__ import annotations

import wave
from pathlib import Path
from typing import Protocol

Span = tuple[float, float]


class VoiceActivityDetector(Protocol):
    def speech_regions(self, audio: Path) -> list[Span]:
        """Speech regions in seconds (original timeline) of a 16 kHz mono WAV."""
        ...


class SileroVad:
    def __init__(
        self,
        threshold: float = 0.5,
        min_speech_ms: int = 100,
        min_silence_ms: int = 100,
        speech_pad_ms: int = 30,
    ) -> None:
        # Short minimums: pause decisions are made by the cut plan, not by the VAD.
        self.threshold = threshold
        self.min_speech_ms = min_speech_ms
        self.min_silence_ms = min_silence_ms
        self.speech_pad_ms = speech_pad_ms

    def speech_regions(self, audio: Path) -> list[Span]:
        import numpy as np
        import torch
        from silero_vad import get_speech_timestamps, load_silero_vad

        with wave.open(str(audio), "rb") as wf:
            if wf.getnchannels() != 1 or wf.getsampwidth() != 2:
                raise ValueError(f"expected 16-bit mono WAV: {audio}")
            rate = wf.getframerate()
            pcm = np.frombuffer(wf.readframes(wf.getnframes()), dtype=np.int16)
        samples = torch.from_numpy(pcm.astype(np.float32) / 32768.0)
        stamps = get_speech_timestamps(
            samples,
            load_silero_vad(),
            threshold=self.threshold,
            sampling_rate=rate,
            min_speech_duration_ms=self.min_speech_ms,
            min_silence_duration_ms=self.min_silence_ms,
            speech_pad_ms=self.speech_pad_ms,
            return_seconds=True,
            time_resolution=3,  # default rounds to 0.1 s, too coarse for cut edges
        )
        return [(float(s["start"]), float(s["end"])) for s in stamps]


class FixedVad:
    """Returns fixed regions. Used in tests."""

    def __init__(self, regions: list[Span]) -> None:
        self.regions = regions

    def speech_regions(self, audio: Path) -> list[Span]:
        return list(self.regions)
