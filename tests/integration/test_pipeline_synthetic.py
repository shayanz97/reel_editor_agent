"""Etappe 1 acceptance on synthetic videos: pauses shortened, no cut inside a word, 1080x1920."""

import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest

from reels.domain.cutplan import CutPlan
from reels.pipeline.process import process_v1
from reels.pipeline.steps import ProjectPaths
from reels.settings_schema import ClientSettings
from tests.fixtures.synthetic_video import make_video
from tests.integration.pipeline_helpers import fakes

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="ffmpeg not installed"),
]

FONTS = Path(__file__).parents[2] / "assets" / "fonts"


def _silences(path: Path) -> list[tuple[float, float]]:
    stderr = subprocess.run(
        ["ffmpeg", "-hide_banner", "-i", str(path), "-af", "silencedetect=noise=-50dB:d=0.2",
         "-f", "null", "-"],
        check=True, capture_output=True, text=True,
    ).stderr  # fmt: skip
    starts = [float(x) for x in re.findall(r"silence_start: ([\d.]+)", stderr)]
    ends = [float(x) for x in re.findall(r"silence_end: ([\d.]+)", stderr)]
    return list(zip(starts, ends, strict=True))


def _video_info(path: Path) -> tuple[int, int, float]:
    data = json.loads(
        subprocess.run(
            ["ffprobe", "-v", "error", "-show_streams", "-show_format", "-of", "json", str(path)],
            check=True,
            capture_output=True,
            text=True,
        ).stdout
    )
    v = next(s for s in data["streams"] if s["codec_type"] == "video")
    return v["width"], v["height"], float(data["format"]["duration"])


@pytest.mark.parametrize(
    ("orientation", "size"), [("portrait", (720, 1280)), ("landscape", (1280, 720))]
)
def test_pipeline_on_synthetic_video(
    tmp_path: Path, orientation: str, size: tuple[int, int]
) -> None:
    spec = make_video(tmp_path / f"{orientation}.mp4", orientation, size=size)  # type: ignore[arg-type]
    engine, vad = fakes(spec)
    settings = ClientSettings()
    paths = ProjectPaths(tmp_path / "project")

    result = process_v1(
        Path(spec.path), paths, settings, [], engine, vad, None, FONTS, render_preset="ultrafast"
    )

    # Cut plan: the 0.3 s pause stays, the 0.8 s and 1.5 s pauses shrink to 2 x edge padding.
    plan = CutPlan.model_validate_json(paths.version(1).cutplan.read_text())
    tones = sum(i.end - i.start for i in spec.intervals if i.kind == "tone")
    pad = plan.params["edge_padding_s"]
    assert plan.output_duration == pytest.approx(tones + 0.3 + 2 * (2 * pad), abs=0.005)
    assert result.cut_count == 2

    # No cut edge inside any word.
    transcript = json.loads(paths.transcript.read_text())
    for r in plan.keep:
        for edge in (r.start, r.end):
            for w in transcript["words"]:
                assert not (w["start"] + 1e-6 < edge < w["end"] - 1e-6), (edge, w)

    # Rendered export: right size and length, long pauses gone, short pause still there.
    width, height, duration = _video_info(result.export)
    assert (width, height) == (1080, 1920)
    assert duration == pytest.approx(plan.output_duration, abs=0.1)
    silences = _silences(result.export)
    assert len(silences) == 1
    assert silences[0][1] - silences[0][0] == pytest.approx(0.3, abs=0.06)

    ass = paths.version(1).subtitles.read_text()
    assert ass.count("Dialogue:") >= 4
    assert "\\k" not in ass


def test_rerun_reuses_transcript_and_karaoke_is_switchable(tmp_path: Path) -> None:
    spec = make_video(tmp_path / "src.mp4", "portrait", size=(360, 640))
    engine, vad = fakes(spec)
    paths = ProjectPaths(tmp_path / "project")
    process_v1(Path(spec.path), paths, ClientSettings(), [], engine, vad, None, FONTS, "ultrafast")
    assert len(engine.calls) == 1

    karaoke = ClientSettings.model_validate({"subtitle": {"karaoke": True}})
    result = process_v1(Path(spec.path), paths, karaoke, [], engine, vad, None, FONTS, "ultrafast")
    assert len(engine.calls) == 1, "transcript must be reused"
    assert "\\k" in paths.version(1).subtitles.read_text()
    assert result.timings["transcribe"] < 1.0


def test_too_long_video_is_rejected(tmp_path: Path) -> None:
    from reels.media.probe import VideoRejectedError

    spec = make_video(tmp_path / "src.mp4", "portrait", size=(360, 640))
    engine, vad = fakes(spec)
    settings = ClientSettings(max_duration_s=2.0)
    with pytest.raises(VideoRejectedError):
        process_v1(
            Path(spec.path), ProjectPaths(tmp_path / "p"), settings, [], engine, vad, None, FONTS
        )
    assert engine.calls == []
