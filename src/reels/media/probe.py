"""Video metadata via ffprobe."""

from __future__ import annotations

import json
from fractions import Fraction
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel

from reels.media.ffmpeg import ffprobe_bin, run

Orientation = Literal["portrait", "landscape", "square"]


class VideoRejectedError(Exception):
    """The video cannot be processed. Message is for operators, `code` selects the client text."""

    def __init__(self, message: str, code: str, **details: float) -> None:
        super().__init__(message)
        self.code = code  # too_long | no_audio | no_video
        self.details = details


class ProbeResult(BaseModel):
    duration: float
    width: int  # display size, rotation already applied
    height: int
    fps: float
    frame_rate: str  # exact rational as reported, e.g. "30000/1001"
    rotation: int  # 0, 90, 180, 270
    has_audio: bool
    video_codec: str | None
    audio_codec: str | None

    @property
    def orientation(self) -> Orientation:
        if self.height > self.width:
            return "portrait"
        if self.width > self.height:
            return "landscape"
        return "square"


def _rotation(stream: dict[str, Any]) -> int:
    rot: float = 0
    for side in stream.get("side_data_list", []):
        if "rotation" in side:
            rot = float(side["rotation"])
    if not rot and "rotate" in stream.get("tags", {}):
        rot = float(stream["tags"]["rotate"])
    return round(rot) % 360


def _rate(stream: dict[str, Any]) -> str:
    for key in ("avg_frame_rate", "r_frame_rate"):
        value = stream.get(key, "0/0")
        num, _, den = value.partition("/")
        if den not in ("", "0") and num not in ("", "0"):
            return value
    return "30/1"


def parse_probe(data: dict[str, Any]) -> ProbeResult:
    streams = data.get("streams", [])
    video = next((s for s in streams if s.get("codec_type") == "video"), None)
    audio = next((s for s in streams if s.get("codec_type") == "audio"), None)
    if video is None:
        raise VideoRejectedError("no video stream", "no_video")
    duration = float(data.get("format", {}).get("duration") or video.get("duration") or 0)
    rotation = _rotation(video)
    width, height = int(video["width"]), int(video["height"])
    if rotation in (90, 270):
        width, height = height, width
    frame_rate = _rate(video)
    return ProbeResult(
        duration=duration,
        width=width,
        height=height,
        fps=float(Fraction(frame_rate)),
        frame_rate=frame_rate,
        rotation=rotation,
        has_audio=audio is not None,
        video_codec=video.get("codec_name"),
        audio_codec=audio.get("codec_name") if audio else None,
    )


def probe(path: Path) -> ProbeResult:
    out = run(
        [ffprobe_bin(), "-v", "error", "-show_streams", "-show_format", "-of", "json", str(path)],
        timeout=60,
    ).stdout
    return parse_probe(json.loads(out))


def check_processable(result: ProbeResult, max_duration_s: float) -> None:
    if result.duration > max_duration_s:
        raise VideoRejectedError(
            f"video is {result.duration:.1f}s long, limit is {max_duration_s:.0f}s",
            "too_long",
            duration_s=result.duration,
            limit_s=max_duration_s,
        )
    if not result.has_audio:
        raise VideoRejectedError("no audio stream", "no_audio")
