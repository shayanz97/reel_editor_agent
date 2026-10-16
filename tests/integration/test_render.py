"""Rendering with real FFmpeg on synthetic videos."""

import json
import shutil
import subprocess
from pathlib import Path

import pytest

from reels.domain.cutplan import KeepRange
from reels.domain.subtitles import SubtitleEvent, SubWord
from reels.media.ass import style_from_settings, write_ass
from reels.media.audio import extract_audio
from reels.media.probe import probe
from reels.media.render import RenderOptions, render
from reels.settings_schema import SubtitleSettings
from tests.fixtures.synthetic_video import make_video

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="ffmpeg not installed"),
]

FONTS = Path(__file__).parents[2] / "assets" / "fonts"
KEEP = [
    KeepRange(start=0.0, end=1.08, first_word=0, last_word=0),
    KeepRange(start=1.72, end=3.08, first_word=1, last_word=1),
]


def _streams(path: Path) -> tuple[dict, dict, float]:  # type: ignore[type-arg]
    out = subprocess.run(
        ["ffprobe", "-v", "error", "-show_streams", "-show_format", "-of", "json", str(path)],
        check=True, capture_output=True, text=True,
    ).stdout  # fmt: skip
    data = json.loads(out)
    v = next(s for s in data["streams"] if s["codec_type"] == "video")
    a = next(s for s in data["streams"] if s["codec_type"] == "audio")
    return v, a, float(data["format"]["duration"])


@pytest.mark.parametrize("orientation", ["portrait", "landscape"])
def test_export_spec(tmp_path: Path, orientation: str) -> None:
    spec = make_video(
        tmp_path / "src.mp4", orientation, size=(720, 1280) if orientation == "portrait" else None
    )  # type: ignore[arg-type]
    src = probe(Path(spec.path))
    out = render(
        Path(spec.path), tmp_path / "out.mp4", KEEP, src, RenderOptions(preset="ultrafast")
    )
    v, a, duration = _streams(out)
    assert (v["width"], v["height"]) == (1080, 1920)
    assert v["codec_name"] == "h264" and v["pix_fmt"] == "yuv420p"
    assert v["avg_frame_rate"] == "30/1"
    assert a["codec_name"] == "aac" and a["sample_rate"] == "48000"
    assert duration == pytest.approx(sum(r.end - r.start for r in KEEP), abs=0.1)


def test_audio_extraction_is_mono_16k_and_reused(tmp_path: Path) -> None:
    spec = make_video(tmp_path / "src.mp4", "portrait", size=(360, 640))
    wav = extract_audio(Path(spec.path), tmp_path / "audio.wav")
    out = subprocess.run(
        ["ffprobe", "-v", "error", "-show_streams", "-show_format", "-of", "json", str(wav)],
        check=True, capture_output=True, text=True,
    ).stdout  # fmt: skip
    data = json.loads(out)
    (a,) = data["streams"]
    assert a["channels"] == 1 and a["sample_rate"] == "16000" and a["codec_name"] == "pcm_s16le"
    assert float(data["format"]["duration"]) == pytest.approx(spec.duration, abs=0.05)
    mtime = wav.stat().st_mtime_ns
    extract_audio(Path(spec.path), wav)
    assert wav.stat().st_mtime_ns == mtime


def _frame_region(video: Path, t: float) -> bytes:
    """Grey pixels of the lower-third subtitle area at time t."""
    return subprocess.run(
        ["ffmpeg", "-v", "error", "-ss", str(t), "-i", str(video), "-frames:v", "1",
         "-vf", "crop=1080:300:0:1200", "-f", "rawvideo", "-pix_fmt", "gray", "-"],
        check=True, capture_output=True,
    ).stdout  # fmt: skip


def test_subtitles_are_burned_in(tmp_path: Path) -> None:
    spec = make_video(tmp_path / "src.mp4", "portrait", size=(720, 1280))
    src = probe(Path(spec.path))
    events = [
        SubtitleEvent(
            start=0.0, end=2.0, words=[SubWord(word_id=0, text="UNTERTITEL", start=0, end=2)]
        )
    ]
    ass = write_ass(tmp_path / "subs.ass", events, style_from_settings(SubtitleSettings()))
    opts = RenderOptions(preset="ultrafast", zoom_on_cuts=False)
    plain = render(Path(spec.path), tmp_path / "plain.mp4", KEEP, src, opts)
    subbed = render(Path(spec.path), tmp_path / "subbed.mp4", KEEP, src, opts, ass, FONTS)

    during_a, during_b = _frame_region(plain, 0.5), _frame_region(subbed, 0.5)
    after_a, after_b = _frame_region(plain, 2.2), _frame_region(subbed, 2.2)
    changed = sum(abs(x - y) > 40 for x, y in zip(during_a, during_b, strict=True))
    assert changed > 2000, "no subtitle pixels found"
    unchanged = sum(abs(x - y) > 40 for x, y in zip(after_a, after_b, strict=True))
    assert unchanged < 200, "subtitle visible after its end time"
