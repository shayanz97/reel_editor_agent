import itertools

from tests.fixtures.synthetic_video import DEFAULT_PATTERN, build_intervals


def test_intervals_are_contiguous_and_cover_pattern() -> None:
    intervals = build_intervals(DEFAULT_PATTERN)
    assert intervals[0].start == 0
    for prev, cur in itertools.pairwise(intervals):
        assert prev.end == cur.start
    assert intervals[-1].end == round(sum(length for _, length in DEFAULT_PATTERN), 3)


def test_default_pattern_has_short_and_long_pauses() -> None:
    pauses = [length for kind, length in DEFAULT_PATTERN if kind == "silence"]
    assert any(p < 0.4 for p in pauses)  # below default pause_threshold_s: must be kept
    assert any(p > 0.4 for p in pauses)  # above: must be shortened
