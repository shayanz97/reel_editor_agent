"""Generate synthetic test videos with FFmpeg (no customer videos in the repo).

Picture: `testsrc2`. Sound: a sine tone with silent gaps at known positions, so tests
can check that pauses are detected and shortened. Each video gets a JSON sidecar with
the exact tone/silence intervals on the original timeline.

    uv run python -m tests.fixtures.synthetic_video --out tests/fixtures/generated
"""

from __future__ import annotations

import argparse
import json
import shutil
import subprocess
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Literal

Kind = Literal["tone", "silence"]

# Mix of pauses: one below the default pause threshold (0.4 s) that must stay,
# and longer ones that must be shortened.
DEFAULT_PATTERN: tuple[tuple[Kind, float], ...] = (
    ("tone", 1.0),
    ("silence", 0.8),
    ("tone", 1.2),
    ("silence", 0.3),
    ("tone", 0.8),
    ("silence", 1.5),
    ("tone", 1.0),
)

SIZES = {"portrait": (1080, 1920), "landscape": (1920, 1080)}


@dataclass(frozen=True)
class Interval:
    kind: Kind
    start: float
    end: float


@dataclass(frozen=True)
class VideoSpec:
    path: str
    width: int
    height: int
    fps: int
    duration: float
    intervals: list[Interval] = field(default_factory=list)

    def silences(self) -> list[Interval]:
        return [i for i in self.intervals if i.kind == "silence"]


def build_intervals(pattern: tuple[tuple[Kind, float], ...]) -> list[Interval]:
    intervals: list[Interval] = []
    t = 0.0
    for kind, length in pattern:
        intervals.append(Interval(kind, round(t, 3), round(t + length, 3)))
        t += length
    return intervals


def _audio_expr(intervals: list[Interval], freq: float = 440.0) -> str:
    gates = "+".join(f"between(t,{i.start},{i.end})" for i in intervals if i.kind == "tone")
    return f"0.5*sin(2*PI*{freq}*t)*({gates})"


def make_video(
    out: Path,
    orientation: Literal["portrait", "landscape"] = "portrait",
    *,
    size: tuple[int, int] | None = None,
    fps: int = 30,
    pattern: tuple[tuple[Kind, float], ...] = DEFAULT_PATTERN,
) -> VideoSpec:
    """Render `out` (mp4) and write `out.with_suffix('.json')`. Returns the spec."""
    if shutil.which("ffmpeg") is None:
        raise RuntimeError("ffmpeg not found on PATH")
    width, height = size or SIZES[orientation]
    intervals = build_intervals(pattern)
    duration = intervals[-1].end
    expr = _audio_expr(intervals)

    out.parent.mkdir(parents=True, exist_ok=True)
    cmd = [
        "ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
        "-f", "lavfi", "-i", f"testsrc2=size={width}x{height}:rate={fps}:duration={duration}",
        "-f", "lavfi", "-i", f"aevalsrc=exprs='{expr}|{expr}':s=48000:d={duration}",
        "-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p",
        "-c:a", "aac", "-ar", "48000",
        "-t", str(duration),
        str(out),
    ]  # fmt: skip
    subprocess.run(cmd, check=True, capture_output=True, text=True)

    spec = VideoSpec(str(out), width, height, fps, duration, intervals)
    out.with_suffix(".json").write_text(json.dumps(asdict(spec), indent=2) + "\n")
    return spec


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--out", type=Path, default=Path("tests/fixtures/generated"))
    args = parser.parse_args()
    for orientation in ("portrait", "landscape"):
        spec = make_video(args.out / f"{orientation}.mp4", orientation)
        print(f"{spec.path}: {spec.width}x{spec.height}, {spec.duration}s")


if __name__ == "__main__":
    main()
