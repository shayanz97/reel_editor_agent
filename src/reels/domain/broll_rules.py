"""B-roll rules on the OUTPUT timeline: where B-roll may go, how long, how much in total."""

from __future__ import annotations

from collections.abc import Sequence

from pydantic import BaseModel

from reels.domain.timeline import Timeline
from reels.domain.words import Word

# Tolerance for float rounding (4.42 - 3.62 = 0.7999999…).
EPS = 1e-6


class BrollRules(BaseModel):
    min_start_s: float = 2.5
    min_duration_s: float = 1.5
    max_duration_s: float = 4.0
    max_coverage: float = 0.25  # share of the output duration
    min_gap_s: float = 3.0


class Candidate(BaseModel):
    key: int  # caller's id (placement number or suggestion index)
    start: float  # output timeline, from the anchored words
    end: float
    priority: int = 2  # 1 = most important


class Slot(BaseModel):
    key: int
    start: float
    end: float

    @property
    def duration(self) -> float:
        return self.end - self.start


def word_span(
    words: Sequence[Word], first_word: int, last_word: int, timeline: Timeline
) -> tuple[float, float] | None:
    """Output span of the kept words between first_word and last_word; None if all are cut."""
    spans = [
        out
        for w in words
        if first_word <= w.id <= last_word and (out := timeline.word_to_output(w)) is not None
    ]
    if not spans:
        return None
    return spans[0][0], spans[-1][1]


def fit(candidate: Candidate, total: float, rules: BrollRules) -> Slot | None:
    """Start at the words (not before min_start_s), length clamped to min..max duration,
    end inside the video. None if no valid slot is left."""
    start = max(candidate.start, rules.min_start_s)
    if start >= candidate.end:
        return None  # the words are over before B-roll is allowed
    length = min(max(candidate.end - start, rules.min_duration_s), rules.max_duration_s)
    end = min(start + length, total)
    if end - start < rules.min_duration_s - EPS:
        start = max(rules.min_start_s, end - rules.min_duration_s)
        if end - start < rules.min_duration_s - EPS:
            return None
    return Slot(key=candidate.key, start=round(start, 3), end=round(end, 3))


def select(candidates: Sequence[Candidate], total: float, rules: BrollRules) -> list[Slot]:
    """Most important first; each slot must keep `min_gap_s` to the others, and all slots
    together stay within `max_coverage` of the video. Result is sorted by start."""
    budget = rules.max_coverage * total
    used = 0.0
    chosen: list[Slot] = []
    for c in sorted(candidates, key=lambda c: (c.priority, c.start, c.key)):
        slot = fit(c, total, rules)
        if slot is None or used + slot.duration > budget + 1e-9:
            continue
        if any(
            slot.start < other.end + rules.min_gap_s and other.start < slot.end + rules.min_gap_s
            for other in chosen
        ):
            continue
        chosen.append(slot)
        used += slot.duration
    return sorted(chosen, key=lambda s: s.start)
