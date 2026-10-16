import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest

from tests.fixtures.synthetic_video import make_video

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="ffmpeg not installed"),
]

# AAC encoder delay and silencedetect granularity shift edges by a few ms.
TOLERANCE_S = 0.06


def _probe(path: Path) -> dict[str, object]:
    out = subprocess.run(
        ["ffprobe", "-v", "error", "-show_streams", "-show_format", "-of", "json", str(path)],
        check=True, capture_output=True, text=True,
    ).stdout  # fmt: skip
    return json.loads(out)  # type: ignore[no-any-return]


def _detect_silences(path: Path) -> list[tuple[float, float]]:
    stderr = subprocess.run(
        ["ffmpeg", "-hide_banner", "-i", str(path), "-af", "silencedetect=noise=-50dB:d=0.2",
         "-f", "null", "-"],
        check=True, capture_output=True, text=True,
    ).stderr  # fmt: skip
    starts = [float(x) for x in re.findall(r"silence_start: ([\d.]+)", stderr)]
    ends = [float(x) for x in re.findall(r"silence_end: ([\d.]+)", stderr)]
    return list(zip(starts, ends, strict=True))


@pytest.mark.parametrize(
    ("orientation", "size"), [("portrait", (1080, 1920)), ("landscape", (1920, 1080))]
)
def test_synthetic_video(tmp_path: Path, orientation: str, size: tuple[int, int]) -> None:
    spec = make_video(tmp_path / f"{orientation}.mp4", orientation)  # type: ignore[arg-type]

    info = _probe(Path(spec.path))
    streams = info["streams"]
    assert isinstance(streams, list)
    video = next(s for s in streams if s["codec_type"] == "video")
    audio = next(s for s in streams if s["codec_type"] == "audio")
    assert (video["width"], video["height"]) == size
    assert video["pix_fmt"] == "yuv420p"
    assert audio["sample_rate"] == "48000"
    assert json.loads((tmp_path / f"{orientation}.json").read_text())["duration"] == spec.duration

    detected = _detect_silences(Path(spec.path))
    expected = [(s.start, s.end) for s in spec.silences()]
    assert len(detected) == len(expected)
    for (d_start, d_end), (e_start, e_end) in zip(detected, expected, strict=True):
        assert d_start == pytest.approx(e_start, abs=TOLERANCE_S)
        assert d_end == pytest.approx(e_end, abs=TOLERANCE_S)
