import pytest

from reels.domain.broll_rules import BrollRules, Candidate, fit, select, word_span
from reels.domain.cutplan import KeepRange
from reels.domain.timeline import Timeline
from tests.unit.helpers import words

R = BrollRules(
    min_start_s=2.5, min_duration_s=1.5, max_duration_s=4.0, max_coverage=0.25, min_gap_s=3.0
)


def _c(key: int, start: float, end: float, priority: int = 2) -> Candidate:
    return Candidate(key=key, start=start, end=end, priority=priority)


@pytest.mark.parametrize(
    ("start", "end", "expected"),
    [
        (5.0, 6.0, (5.0, 6.5)),  # too short -> min duration
        (5.0, 12.0, (5.0, 9.0)),  # too long -> max duration
        (1.0, 4.0, (2.5, 4.0)),  # starts before min_start -> moved
        (0.5, 2.0, None),  # words over before B-roll is allowed
        (19.5, 20.0, (18.5, 20.0)),  # at the very end: moved back to fit
    ],
)
def test_fit(start: float, end: float, expected: tuple[float, float] | None) -> None:
    slot = fit(_c(0, start, end), total=20.0, rules=R)
    assert (None if slot is None else (slot.start, slot.end)) == (
        None if expected is None else pytest.approx(expected)
    )


def test_fit_none_when_video_too_short() -> None:
    assert fit(_c(0, 2.6, 3.0), total=3.0, rules=R) is None


def test_select_priority_gap_and_coverage() -> None:
    cands = [
        _c(0, 3.0, 5.0, priority=2),
        _c(1, 4.0, 6.0, priority=1),  # overlaps 0, more important -> wins
        _c(2, 12.0, 14.0, priority=3),
        _c(3, 20.0, 22.0, priority=2),
        _c(4, 30.0, 34.0, priority=2),
    ]
    slots = select(cands, total=40.0, rules=R)  # budget: 10 s
    assert [s.key for s in slots] == [1, 2, 3, 4]
    assert sum(s.duration for s in slots) <= 10.0
    tighter = select(cands, total=40.0, rules=R.model_copy(update={"max_coverage": 0.1}))
    assert [s.key for s in tighter] == [1, 3]  # 4 s budget: the two best that fit


def test_select_keeps_min_gap() -> None:
    slots = select([_c(0, 5.0, 6.5), _c(1, 8.0, 9.5)], total=60.0, rules=R)
    assert len(slots) == 1
    assert len(select([_c(0, 5.0, 6.5), _c(1, 9.6, 11.0)], total=60.0, rules=R)) == 2


def test_word_span_uses_output_time_and_drops_cut_words() -> None:
    ws = words(("a", 1.0, 1.5), ("b", 2.0, 2.5), ("c", 10.0, 10.5))
    tl = Timeline([
        KeepRange(start=0.9, end=2.6, first_word=0, last_word=1),
        KeepRange(start=9.9, end=10.6, first_word=2, last_word=2),
    ])  # fmt: skip
    assert word_span(ws, 0, 1, tl) == pytest.approx((0.1, 1.6))
    assert word_span(ws, 1, 2, tl) == pytest.approx((1.1, 2.3))
    cut = Timeline([KeepRange(start=9.9, end=10.6, first_word=2, last_word=2)])
    assert word_span(ws, 0, 1, cut) is None  # deleted segment -> its B-roll disappears


def test_fit_exact_min_duration_despite_float_rounding() -> None:
    rules = R.model_copy(update={"min_duration_s": 0.8, "max_duration_s": 1.2, "min_start_s": 0.5})
    slot = fit(_c(0, 3.62, 4.42), total=4.62, rules=rules)  # 4.42 - 3.62 == 0.7999999…
    assert slot is not None and (slot.start, slot.end) == (3.62, 4.42)
