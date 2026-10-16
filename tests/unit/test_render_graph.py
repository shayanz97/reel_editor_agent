from pathlib import Path

import pytest

from reels.domain.cutplan import KeepRange
from reels.media.probe import ProbeResult
from reels.media.render import RenderOptions, build_filter_graph, filter_path, output_frame_rate


def _src(rate: str = "30/1") -> ProbeResult:
    return ProbeResult(
        duration=10, width=1920, height=1080, fps=30, frame_rate=rate, rotation=0,
        has_audio=True, video_codec="h264", audio_codec="aac",
    )  # fmt: skip


KEEP = [
    KeepRange(start=0.5, end=2.0, first_word=0, last_word=3),
    KeepRange(start=3.0, end=3.01, first_word=4, last_word=4),
    KeepRange(start=4.0, end=6.0, first_word=5, last_word=9),
]


def test_graph_structure() -> None:
    g = build_filter_graph(KEEP, _src(), RenderOptions())
    assert "split=3[vs0][vs1][vs2]" in g
    assert "[vs0]trim=start=0.500:end=2.000,setpts=PTS-STARTPTS,setsar=1[v0]" in g
    assert "[as2]atrim=start=4.000:end=6.000" in g
    assert "concat=n=3:v=1:a=1[vc][ac]" in g
    assert "loudnorm=I=-14:" in g
    assert g.index("concat") < g.index("[vout]")


def test_zoom_alternates() -> None:
    g = build_filter_graph(KEEP, _src(), RenderOptions(zoom_factor=1.08))
    chains = {c.split("]")[0] + "]": c for c in g.split(";")}
    assert "scale=1166:2074" not in chains["[vs0]"]
    assert "scale=1166:2074,crop=1080:1920" in chains["[vs1]"]
    assert "scale=1166:2074" not in chains["[vs2]"]
    assert "scale=1166" not in build_filter_graph(KEEP, _src(), RenderOptions(zoom_on_cuts=False))


def test_fades_skipped_for_very_short_ranges() -> None:
    g = build_filter_graph(KEEP, _src(), RenderOptions(audio_fade_ms=15))
    chains = g.split(";")
    a0 = next(c for c in chains if c.startswith("[as0]"))
    a1 = next(c for c in chains if c.startswith("[as1]"))
    assert "afade=t=in:st=0:d=0.015" in a0 and "afade=t=out:st=1.485:d=0.015" in a0
    assert "afade" not in a1


def test_subtitles_are_last_video_filter(tmp_path: Path) -> None:
    g = build_filter_graph(KEEP, _src(), RenderOptions(), tmp_path / "s.ass", tmp_path)
    vout = next(c for c in g.split(";") if c.endswith("[vout]"))
    assert vout.startswith("[vc]fps=30/1,subtitles=filename='")
    assert f":fontsdir='{tmp_path.resolve()}'[vout]" in vout


@pytest.mark.parametrize(
    ("rate", "out"),
    [("30/1", "30/1"), ("30000/1001", "30000/1001"), ("60/1", "30"), ("25/1", "25/1")],
)
def test_output_frame_rate(rate: str, out: str) -> None:
    assert output_frame_rate(_src(rate), 30) == out


def test_unsafe_paths_rejected() -> None:
    with pytest.raises(ValueError):
        filter_path(Path("/tmp/it's.ass"))
    with pytest.raises(ValueError):
        filter_path(Path("/tmp/a:b.ass"))


def test_empty_plan_rejected() -> None:
    with pytest.raises(ValueError):
        build_filter_graph([], _src(), RenderOptions())
