"""Comparing two transcripts of the same audio (engine benchmark)."""

from __future__ import annotations

import statistics
from collections.abc import Sequence
from difflib import SequenceMatcher

from pydantic import BaseModel

from reels.domain.words import Word, normalize_token


class DiffStats(BaseModel):
    mean: float
    median: float
    p90: float
    max: float


class TextDiff(BaseModel):
    a: str
    b: str


class Comparison(BaseModel):
    words_a: int
    words_b: int
    matched: int
    text_similarity: float  # 0..1 on normalized tokens
    start_diff_s: DiffStats | None  # absolute start-time difference of matched words
    end_diff_s: DiffStats | None
    differences: list[TextDiff]


def _stats(values: Sequence[float]) -> DiffStats | None:
    if not values:
        return None
    ordered = sorted(values)
    p90 = ordered[min(len(ordered) - 1, round(0.9 * (len(ordered) - 1)))]
    return DiffStats(
        mean=statistics.fmean(ordered), median=statistics.median(ordered), p90=p90, max=ordered[-1]
    )


def compare_words(a: Sequence[Word], b: Sequence[Word]) -> Comparison:
    """Match words by normalized text (difflib) and compare timings of the matched pairs."""
    tokens_a = [normalize_token(w.text) for w in a]
    tokens_b = [normalize_token(w.text) for w in b]
    matcher = SequenceMatcher(a=tokens_a, b=tokens_b, autojunk=False)
    starts: list[float] = []
    ends: list[float] = []
    for block in matcher.get_matching_blocks():
        for k in range(block.size):
            wa, wb = a[block.a + k], b[block.b + k]
            starts.append(abs(wa.start - wb.start))
            ends.append(abs(wa.end - wb.end))
    differences = [
        TextDiff(
            a=" ".join(w.text for w in a[i1:i2]),
            b=" ".join(w.text for w in b[j1:j2]),
        )
        for tag, i1, i2, j1, j2 in matcher.get_opcodes()
        if tag != "equal"
    ]
    return Comparison(
        words_a=len(a),
        words_b=len(b),
        matched=len(starts),
        text_similarity=matcher.ratio(),
        start_diff_s=_stats(starts),
        end_diff_s=_stats(ends),
        differences=differences,
    )
