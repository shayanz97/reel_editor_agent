import pytest

from reels.domain.cutplan import CutParams, KeepRange, build_cut_plan, filler_ids
from tests.unit.helpers import assert_no_cut_inside_words, words

P = CutParams(pause_threshold_s=0.4, pause_target_s=0.15, cut_padding_s=0.08)
PAD = P.edge_padding_s  # 0.08


def _bounds(plan_keep: list[KeepRange]) -> list[float]:
    return [b for r in plan_keep for b in (r.start, r.end)]


def test_short_pauses_are_kept_long_pauses_shortened() -> None:
    ws = words(("a", 1.0, 1.5), ("b", 1.8, 2.2), ("c", 3.5, 4.0))
    plan = build_cut_plan(ws, [], duration=5.0, params=P)
    assert [(r.start, r.end) for r in plan.keep] == [
        pytest.approx((1.0 - PAD, 2.2 + PAD)),
        pytest.approx((3.5 - PAD, 4.0 + PAD)),
    ]
    assert (plan.keep[0].first_word, plan.keep[0].last_word) == (0, 1)
    assert (plan.keep[1].first_word, plan.keep[1].last_word) == (2, 2)
    assert plan.cut_count == 1
    # shortened pause = 2 * edge padding
    assert plan.keep[1].start - plan.keep[0].end > 0
    assert plan.output_duration == pytest.approx((2.2 - 1.0 + 2 * PAD) + (0.5 + 2 * PAD))


def test_pause_exactly_at_threshold_is_kept() -> None:
    ws = words(("a", 0.0, 1.0), ("b", 1.4, 2.0))
    plan = build_cut_plan(ws, [], duration=2.0, params=P)
    assert len(plan.keep) == 1


def test_padding_clamped_to_video_bounds() -> None:
    ws = words(("a", 0.02, 0.5), ("b", 0.6, 0.98))
    plan = build_cut_plan(ws, [], duration=1.0, params=P)
    assert (plan.keep[0].start, plan.keep[0].end) == (0.0, 1.0)


def test_vad_extends_word_tail() -> None:
    ws = words(("a", 1.0, 1.4), ("b", 3.0, 3.5))
    plan = build_cut_plan(ws, [(0.95, 1.7), (2.9, 3.6)], duration=5.0, params=P)
    assert plan.keep[0].end == pytest.approx(1.7 + PAD)
    assert plan.keep[1].start == pytest.approx(2.9 - PAD)


def test_vad_only_sound_far_from_words_is_dropped() -> None:
    ws = words(("a", 1.0, 1.4))
    plan = build_cut_plan(ws, [(3.0, 3.5)], duration=5.0, params=P)
    assert len(plan.keep) == 1


def test_vad_sound_close_to_words_is_kept() -> None:
    ws = words(("a", 1.0, 1.4))
    plan = build_cut_plan(ws, [(1.6, 1.9)], duration=5.0, params=P)
    assert plan.keep[0].end == pytest.approx(1.9 + PAD)


def test_filler_is_removed_and_no_cut_inside_any_word() -> None:
    ws = words(("Das", 1.0, 1.3), ("ähm", 1.35, 1.7), ("ist", 1.75, 2.0), ("gut.", 2.05, 2.4))
    fillers = filler_ids(ws)
    assert fillers == {1}
    # VAD sees the filler as speech; it must not protect it.
    plan = build_cut_plan(ws, [(0.95, 2.45)], duration=3.0, params=P, removed=fillers)
    assert len(plan.keep) == 2
    assert plan.keep[0].end <= 1.35
    assert plan.keep[1].start >= 1.7
    assert_no_cut_inside_words(_bounds(plan.keep), ws)
    assert (plan.keep[0].first_word, plan.keep[0].last_word) == (0, 0)
    assert (plan.keep[1].first_word, plan.keep[1].last_word) == (2, 3)


def test_removed_word_touching_neighbours_cuts_exactly_at_edges() -> None:
    ws = words(("a", 1.0, 1.5), ("hm", 1.5, 1.8), ("b", 1.8, 2.2))
    plan = build_cut_plan(ws, [], duration=3.0, params=P, removed={1})
    assert plan.keep[0].end == 1.5
    assert plan.keep[1].start == 1.8


def test_no_kept_words_gives_empty_plan() -> None:
    ws = words(("ähm", 1.0, 1.5))
    plan = build_cut_plan(ws, [(1.0, 1.5)], duration=2.0, params=P, removed={0})
    assert plan.keep == []
    assert plan.output_duration == 0


def test_params_are_recorded() -> None:
    plan = build_cut_plan(words(("a", 0, 1)), [], duration=1.0, params=P)
    assert plan.params["pause_threshold_s"] == 0.4
    assert plan.params["edge_padding_s"] == PAD


def test_ranges_sorted_and_non_overlapping_on_dense_input() -> None:
    spec = []
    t = 0.3
    for i in range(60):
        spec.append((f"w{i}", t, t + 0.25))
        t += 0.25 + (0.9 if i % 7 == 0 else 0.1 if i % 3 else 0.45)
    ws = words(*spec)
    plan = build_cut_plan(ws, [], duration=t + 1, params=P, removed={5, 6, 20})
    for prev, cur in zip(plan.keep, plan.keep[1:], strict=False):
        assert prev.end < cur.start
    assert_no_cut_inside_words(_bounds(plan.keep), ws)


def test_edge_padding_at_least_half_target() -> None:
    assert CutParams(pause_target_s=0.3, cut_padding_s=0.05).edge_padding_s == 0.15
