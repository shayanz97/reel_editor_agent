"""Silero VAD on real speech, generated locally with macOS `say` (bundled model, no download)."""

import itertools
import shutil
import subprocess
from pathlib import Path

import pytest

from reels.media.audio import extract_audio
from reels.vad.silero import SileroVad

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(shutil.which("say") is None, reason="macOS `say` not available"),
    pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="ffmpeg not installed"),
]


def test_silero_finds_speech_around_a_pause(tmp_path: Path) -> None:
    aiff = tmp_path / "speech.aiff"
    subprocess.run(
        ["say", "-o", str(aiff), "Hallo zusammen, heute geht es um Container. [[slnc 1500]] "
         "Und jetzt der zweite Teil."],
        check=True,
    )  # fmt: skip
    wav = extract_audio(aiff, tmp_path / "speech.wav")
    regions = SileroVad().speech_regions(wav)
    assert len(regions) >= 2
    gaps = [b[0] - a[1] for a, b in itertools.pairwise(regions)]
    assert max(gaps) == pytest.approx(1.5, abs=0.3)
