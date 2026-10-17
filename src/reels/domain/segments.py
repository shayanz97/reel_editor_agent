"""Segments: sentences / units of meaning, numbered per version for the preview."""

from __future__ import annotations

from collections.abc import Collection, Sequence

from pydantic import BaseModel

from reels.domain.words import TextOverlay, Word, display_text

SENTENCE_END = (".", "!", "?", "…")
CLAUSE_END = (",", ";", ":", "–")
CLOSING = "\"'“”»«)]"

DEFAULT_MAX_SEGMENT_WORDS = 30


class Span(BaseModel):
    """A segment on the transcript, independent of any version (stable word range)."""

    first_word: int
    last_word: int


class Segment(BaseModel):
    number: int  # 1..n within a version, only segments with at least one kept word
    first_word: int
    last_word: int
    text: str


def _ends_with(text: str, marks: tuple[str, ...]) -> bool:
    return text.rstrip(CLOSING).endswith(marks)


def split_sentences(
    words: Sequence[Word],
    max_words: int = DEFAULT_MAX_SEGMENT_WORDS,
    splits_after: Collection[int] = (),
) -> list[Span]:
    """Split the transcript at sentence punctuation.

    Sentences longer than `max_words` are split at the clause mark (comma etc.) closest to
    their middle, recursively; without any clause mark they are split in the middle.
    `splits_after` forces a segment end after these word ids (correction `split_segment`).
    Depends only on the transcript and those splits, so spans are stable across versions.
    """
    spans: list[Span] = []
    start = 0
    for i, w in enumerate(words):
        if _ends_with(w.text, SENTENCE_END) or w.id in splits_after or i == len(words) - 1:
            spans.extend(_split_long(words[start : i + 1], max_words))
            start = i + 1
    return spans


def _split_long(words: Sequence[Word], max_words: int) -> list[Span]:
    if not words:
        return []
    if len(words) <= max_words:
        return [Span(first_word=words[0].id, last_word=words[-1].id)]
    middle = len(words) / 2
    candidates = [i for i, w in enumerate(words[:-1]) if _ends_with(w.text, CLAUSE_END)]
    cut = min(candidates, key=lambda i: abs(i + 1 - middle)) + 1 if candidates else len(words) // 2
    return _split_long(words[:cut], max_words) + _split_long(words[cut:], max_words)


def number_segments(
    words: Sequence[Word],
    spans: Sequence[Span],
    kept_ids: Collection[int],
    overlay: TextOverlay | None = None,
) -> list[Segment]:
    """Number the spans that still contain at least one kept, visible word (1, 2, 3 …)."""
    by_id = {w.id: w for w in words}
    segments: list[Segment] = []
    for span in spans:
        ids = range(span.first_word, span.last_word + 1)
        visible = [
            by_id[i]
            for i in ids
            if i in kept_ids and i in by_id and display_text(by_id[i], overlay).strip()
        ]
        if not visible:
            continue
        text = " ".join(display_text(w, overlay) for w in visible)
        segments.append(
            Segment(
                number=len(segments) + 1,
                first_word=span.first_word,
                last_word=span.last_word,
                text=text,
            )
        )
    return segments
