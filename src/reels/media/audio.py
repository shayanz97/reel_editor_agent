"""Audio extraction for transcription and VAD."""

from __future__ import annotations

from pathlib import Path

from reels.media.ffmpeg import ffmpeg_bin, run

SAMPLE_RATE = 16_000


def extract_audio(src: Path, dst: Path, sample_rate: int = SAMPLE_RATE) -> Path:
    """Mono 16-bit PCM WAV. Reuses `dst` if it is newer than `src` (idempotent step)."""
    if dst.exists() and dst.stat().st_mtime >= src.stat().st_mtime and dst.stat().st_size > 44:
        return dst
    dst.parent.mkdir(parents=True, exist_ok=True)
    tmp = dst.with_suffix(".tmp.wav")
    cmd = [
        ffmpeg_bin(), "-hide_banner", "-loglevel", "error", "-y",
        "-i", str(src),
        "-vn", "-ac", "1", "-ar", str(sample_rate), "-c:a", "pcm_s16le",
        str(tmp),
    ]  # fmt: skip
    run(cmd, timeout=600)
    tmp.replace(dst)
    return dst
