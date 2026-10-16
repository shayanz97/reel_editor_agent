"""Running FFmpeg/ffprobe: argument lists only (never shell=True), stderr kept for job logs."""

from __future__ import annotations

import subprocess
from collections.abc import Sequence

from reels.config import get_settings

STDERR_TAIL_LINES = 40


class FFmpegError(RuntimeError):
    def __init__(self, cmd: Sequence[str], returncode: int, stderr: str) -> None:
        self.cmd = list(cmd)
        self.returncode = returncode
        self.stderr_tail = "\n".join(stderr.strip().splitlines()[-STDERR_TAIL_LINES:])
        super().__init__(f"{cmd[0]} exited with {returncode}: {self.stderr_tail[-500:]}")


def ffmpeg_bin() -> str:
    return get_settings().ffmpeg_bin


def ffprobe_bin() -> str:
    return get_settings().ffprobe_bin


def run(cmd: Sequence[str], timeout: float | None = None) -> subprocess.CompletedProcess[str]:
    try:
        result = subprocess.run(
            list(cmd), capture_output=True, text=True, timeout=timeout, check=False
        )
    except FileNotFoundError as exc:
        raise FFmpegError(cmd, -1, f"executable not found: {cmd[0]}") from exc
    if result.returncode != 0:
        raise FFmpegError(cmd, result.returncode, result.stderr)
    return result
