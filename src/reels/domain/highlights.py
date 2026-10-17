"""Highlights: one decision per segment (from the LLM), validated against the current version."""

from __future__ import annotations

from collections.abc import Collection, Iterable, Sequence
from typing import Literal

from pydantic import BaseModel

from reels.domain.segments import Segment
from reels.domain.words import TextOverlay, Word, display_text

HighlightKind = Literal["structure", "key", "none"]

# A highlight is one contiguous phrase: "Investieren", "A-Tier", "100 Jahre", "Geld verdienen".
MAX_PHRASE_WORDS = 4


class HighlightChoice(BaseModel):
    segment: int  # segment number (1, 2, 3 … as in the preview)
    kind: HighlightKind
    first_word: int | None
    last_word: int | None


class HighlightSelection(BaseModel):
    word_ids: list[int]
    missing_segments: int  # segments the suggestion did not decide on
    rejected: int  # choices that failed validation (never guessed into something else)


def transcript_for_prompt(
    words: Sequence[Word], segments: Sequence[Segment], overlay: TextOverlay | None = None
) -> str:
    """`<segment>: [id]word [id]word …`, one segment per line; hidden words are left out."""
    by_id = {w.id: w for w in words}
    lines: list[str] = []
    for seg in segments:
        tokens = [
            f"[{i}]{display_text(by_id[i], overlay)}"
            for i in range(seg.first_word, seg.last_word + 1)
            if i in by_id and display_text(by_id[i], overlay).strip()
        ]
        lines.append(f"{seg.number}: " + " ".join(tokens))
    return "\n".join(lines)


def select_highlights(
    choices: Iterable[HighlightChoice],
    segments: Sequence[Segment],
    kept: Collection[int],
    visible: Collection[int],
    max_words: int = MAX_PHRASE_WORDS,
) -> HighlightSelection:
    """Turn per-segment choices into highlighted word ids.

    A choice is used only if its phrase lies inside its segment, is at most `max_words` long
    and was not cut away. Hidden words inside the phrase (multi-word glossary replacements)
    stay hidden. The first choice per segment wins; anything invalid is dropped, not fixed.
    """
    by_number = {s.number: s for s in segments}
    decided: set[int] = set()
    ids: set[int] = set()
    rejected = 0
    for choice in choices:
        segment = by_number.get(choice.segment)
        if segment is None or choice.segment in decided:
            rejected += 1
            continue
        decided.add(choice.segment)
        if choice.kind == "none":
            continue
        first, last = choice.first_word, choice.last_word
        if first is None or last is None:
            rejected += 1
            continue
        span = range(first, last + 1)
        if (
            first > last
            or first < segment.first_word
            or last > segment.last_word
            or len(span) > max_words
            or any(i not in kept for i in span)
        ):
            rejected += 1
            continue
        ids.update(i for i in span if i in visible)
    return HighlightSelection(
        word_ids=sorted(ids),
        missing_segments=len(by_number.keys() - decided),
        rejected=rejected,
    )
