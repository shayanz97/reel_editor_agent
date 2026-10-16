"""Words with stable ids: the shared anchor for cut plans, subtitles and corrections."""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence

from pydantic import BaseModel, ConfigDict


class Word(BaseModel):
    model_config = ConfigDict(frozen=True)

    id: int
    text: str
    start: float  # seconds, ORIGINAL timeline
    end: float
    score: float | None = None


class RawWord(BaseModel):
    """Engine output before ids are assigned. Times may be missing (e.g. numbers in WhisperX)."""

    text: str
    start: float | None = None
    end: float | None = None
    score: float | None = None


class Transcript(BaseModel):
    engine: str
    model: str | None = None
    language: str = "de"
    words: list[Word]

    @property
    def text(self) -> str:
        return " ".join(w.text for w in self.words)


# Text corrections per word id. An empty string hides the word in subtitles (it stays timed).
TextOverlay = Mapping[int, str]

_EDGE_PUNCT = re.compile(r"^[^\w]+|[^\w]+$", re.UNICODE)


def normalize_token(text: str) -> str:
    """Lower-cased token without edge punctuation (``„Kubernetes,“`` -> ``kubernetes``)."""
    return _EDGE_PUNCT.sub("", text.strip()).casefold()


def display_text(word: Word, overlay: TextOverlay | None = None) -> str:
    if overlay is not None and word.id in overlay:
        return overlay[word.id]
    return word.text


def build_words(raw: Sequence[RawWord]) -> list[Word]:
    """Assign ids 0..n-1 and repair timings so that words are ordered and non-overlapping.

    - Empty tokens are dropped.
    - Missing times are interpolated between the neighbouring known times; at the edges
      they collapse onto the nearest known time (so no extra silence is protected).
    - Overlaps between consecutive words are resolved by moving the later start.
    """
    items = [r for r in raw if r.text.strip()]
    starts = _interpolate([r.start for r in items], [r.end for r in items])
    words: list[Word] = []
    prev_end = 0.0
    for i, (r, (start, end)) in enumerate(zip(items, starts, strict=True)):
        start = max(start, prev_end)
        end = max(end, start)
        words.append(Word(id=i, text=r.text.strip(), start=start, end=end, score=r.score))
        prev_end = end
    return words


def _interpolate(
    starts: Sequence[float | None], ends: Sequence[float | None]
) -> list[tuple[float, float]]:
    """Fill missing (start, end) pairs by spreading the gap between known neighbours evenly."""
    n = len(starts)
    result: list[tuple[float, float] | None] = [
        (s, e) if s is not None and e is not None else None
        for s, e in zip(starts, ends, strict=True)
    ]
    i = 0
    while i < n:
        if result[i] is not None:
            i += 1
            continue
        j = i
        while j < n and result[j] is None:
            j += 1
        prev = result[i - 1] if i > 0 else None
        nxt = result[j] if j < n else None
        left = prev[1] if prev is not None else (nxt[0] if nxt is not None else 0.0)
        right = nxt[0] if nxt is not None else left
        step = (right - left) / (j - i)
        for k in range(i, j):
            result[k] = (left + step * (k - i), left + step * (k - i + 1))
        i = j
    return [r if r is not None else (0.0, 0.0) for r in result]
