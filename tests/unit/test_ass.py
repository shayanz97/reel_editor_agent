import os
from pathlib import Path

import pytest

from reels.domain.subtitles import SubtitleEvent, SubWord
from reels.media.ass import (
    ass_colour,
    ass_override_colour,
    ass_time,
    escape_text,
    render_ass,
    style_from_settings,
)
from reels.settings_schema import SubtitleSettings

GOLDEN = Path(__file__).parents[1] / "fixtures" / "golden"


def _events() -> list[SubtitleEvent]:
    return [
        SubtitleEvent(
            start=0.08,
            end=1.2,
            words=[
                SubWord(word_id=0, text="Heute", start=0.08, end=0.4),
                SubWord(word_id=1, text="geht's", start=0.45, end=0.7),
                SubWord(word_id=2, text="um", start=0.72, end=0.8),
                SubWord(word_id=3, text="Kubernetes.", start=0.82, end=1.2, highlight=True),
            ],
        ),
        SubtitleEvent(
            start=1.2,
            end=2.05,
            words=[
                SubWord(word_id=4, text="{Los}", start=1.2, end=1.5),
                SubWord(word_id=5, text="geht's!", start=1.55, end=1.8),
            ],
        ),
    ]


def _check_golden(name: str, content: str) -> None:
    path = GOLDEN / name
    if os.environ.get("UPDATE_GOLDEN"):
        path.write_text(content, encoding="utf-8")
    assert content == path.read_text(encoding="utf-8")


@pytest.mark.parametrize("karaoke", [False, True])
def test_ass_golden(karaoke: bool) -> None:
    style = style_from_settings(SubtitleSettings())
    content = render_ass(_events(), style, karaoke=karaoke)
    _check_golden(f"neutral{'_karaoke' if karaoke else ''}.ass", content)


def test_position_presets() -> None:
    assert style_from_settings(SubtitleSettings(position="center")).alignment == 5
    assert style_from_settings(SubtitleSettings(position="upper_third")).alignment == 8
    with pytest.raises(ValueError):
        style_from_settings(SubtitleSettings(style="fancy"))


def test_karaoke_durations_sum_to_event_length() -> None:
    import re

    content = render_ass(_events(), style_from_settings(SubtitleSettings()), karaoke=True)
    first = next(line for line in content.splitlines() if line.startswith("Dialogue"))
    assert sum(int(k) for k in re.findall(r"\\k(\d+)", first)) == 112  # 0.08 -> 1.20


@pytest.mark.parametrize(
    ("hex_rgb", "ass"),
    [("#FFFFFF", "&H00FFFFFF"), ("#FFD60A", "&H000AD6FF"), ("#102030", "&H00302010")],
)
def test_ass_colour(hex_rgb: str, ass: str) -> None:
    assert ass_colour(hex_rgb) == ass


def test_ass_colour_alpha_and_invalid() -> None:
    assert ass_colour("#000000", alpha=0x80) == "&H80000000"
    with pytest.raises(ValueError):
        ass_colour("#FFF")


@pytest.mark.parametrize(
    ("t", "s"),
    [(0, "0:00:00.00"), (1.234, "0:00:01.23"), (61.5, "0:01:01.50"), (3725.999, "1:02:06.00")],
)
def test_ass_time(t: float, s: str) -> None:
    assert ass_time(t) == s


def test_escape_text() -> None:
    assert escape_text("a{b}\\c\nd") == "a(b)/c d"


def test_override_colour() -> None:
    assert ass_override_colour("#FFD60A") == "&H0AD6FF&"
