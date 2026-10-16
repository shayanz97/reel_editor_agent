"""Mapping from the ORIGINAL timeline to the OUTPUT timeline (the cut video)."""

from __future__ import annotations

from bisect import bisect_right
from collections.abc import Sequence

from reels.domain.cutplan import KeepRange
from reels.domain.words import Word

EPS = 1e-6


class Timeline:
    def __init__(self, keep: Sequence[KeepRange]) -> None:
        self._ranges = list(keep)
        self._starts = [r.start for r in self._ranges]
        self._offsets: list[float] = []
        offset = 0.0
        for r in self._ranges:
            self._offsets.append(offset)
            offset += r.end - r.start
        self.duration = offset

    def _index(self, t: float) -> int | None:
        i = bisect_right(self._starts, t + EPS) - 1
        if i < 0:
            return None
        r = self._ranges[i]
        return i if t <= r.end + EPS else None

    def to_output(self, t: float) -> float | None:
        """Output time of original time `t`, or None if `t` was cut away."""
        i = self._index(t)
        if i is None:
            return None
        r = self._ranges[i]
        return self._offsets[i] + min(max(t - r.start, 0.0), r.end - r.start)

    def word_to_output(self, word: Word) -> tuple[float, float] | None:
        """Output span of a word, or None if the word is not kept.

        A word counts as kept if its start lies in a kept range; its end is clipped to that
        range (cut plans never cut inside a kept word, so this only absorbs rounding).
        """
        i = self._index(word.start)
        if i is None:
            return None
        r = self._ranges[i]
        start = self._offsets[i] + max(word.start - r.start, 0.0)
        end = self._offsets[i] + min(word.end, r.end) - r.start
        return start, max(end, start)

    def kept_word_ids(self, words: Sequence[Word]) -> set[int]:
        return {w.id for w in words if self.word_to_output(w) is not None}

    def cut_times(self) -> list[float]:
        """Output times of the joins between kept ranges."""
        return self._offsets[1:]
