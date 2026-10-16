import pytest

from reels.domain.cutplan import KeepRange
from reels.domain.timeline import Timeline
from reels.domain.words import Word


def _tl() -> Timeline:
    return Timeline(
        [
            KeepRange(start=1.0, end=2.0, first_word=0, last_word=0),
            KeepRange(start=3.0, end=3.5, first_word=1, last_word=1),
            KeepRange(start=5.0, end=6.0, first_word=2, last_word=2),
        ]
    )


def test_duration_and_cut_times() -> None:
    tl = _tl()
    assert tl.duration == pytest.approx(2.5)
    assert tl.cut_times() == pytest.approx([1.0, 1.5])


@pytest.mark.parametrize(
    ("t", "out"),
    [(1.0, 0.0), (1.5, 0.5), (2.0, 1.0), (3.0, 1.0), (3.25, 1.25), (3.5, 1.5), (6.0, 2.5)],
)
def test_to_output_inside_ranges(t: float, out: float) -> None:
    assert _tl().to_output(t) == pytest.approx(out)


@pytest.mark.parametrize("t", [0.0, 0.999, 2.5, 4.0, 6.01, 100.0])
def test_to_output_outside_ranges_is_none(t: float) -> None:
    assert _tl().to_output(t) is None


def test_word_mapping_and_kept_ids() -> None:
    tl = _tl()
    ws = [
        Word(id=0, text="a", start=1.2, end=1.8),
        Word(id=1, text="b", start=2.2, end=2.6),  # cut away
        Word(id=2, text="c", start=5.1, end=5.9),
    ]
    assert tl.word_to_output(ws[0]) == pytest.approx((0.2, 0.8))
    assert tl.word_to_output(ws[1]) is None
    assert tl.word_to_output(ws[2]) == pytest.approx((1.6, 2.4))
    assert tl.kept_word_ids(ws) == {0, 2}


def test_word_end_is_clipped_to_its_range() -> None:
    tl = _tl()
    assert tl.word_to_output(Word(id=0, text="x", start=3.4, end=3.6)) == pytest.approx((1.4, 1.5))


def test_empty_timeline() -> None:
    tl = Timeline([])
    assert tl.duration == 0
    assert tl.to_output(1.0) is None
    assert tl.cut_times() == []
