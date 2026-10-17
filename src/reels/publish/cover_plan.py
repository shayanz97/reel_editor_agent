"""Choice of 3 cover frames + headlines (LLM suggestion, validated, with fallback)."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass

COVER_COUNT = 3
MAX_HEADLINE_CHARS = 60
FALLBACK_WORDS = 6


@dataclass(frozen=True)
class PlannedCover:
    number: int  # 1..3
    candidate: int  # index into the candidate list (0-based)
    headline: str


def fallback_headline(first_sentence: str) -> str:
    """First words of the video's first sentence, without trailing punctuation."""
    words = first_sentence.split()[:FALLBACK_WORDS]
    return " ".join(words).rstrip(".,;:!?…") or "Neues Video"


def _clean(headline: str) -> str:
    text = " ".join(headline.split()).strip("\"'„“”")
    if len(text) > MAX_HEADLINE_CHARS:
        text = text[:MAX_HEADLINE_CHARS].rsplit(" ", 1)[0]
    return text


def plan_covers(
    suggestions: Sequence[tuple[int, str]], candidate_count: int, first_sentence: str
) -> list[PlannedCover]:
    """`suggestions` are (image number 1..n, headline). Invalid or duplicate images/headlines
    are dropped; missing covers are filled with the best remaining candidates (they are
    ordered by time, so this spreads over the video) and a fallback headline."""
    used_images: set[int] = set()
    used_headlines: set[str] = set()
    chosen: list[tuple[int, str]] = []
    for image, headline in suggestions:
        index = image - 1
        text = _clean(headline)
        if not 0 <= index < candidate_count or index in used_images:
            continue
        if not text or text.casefold() in used_headlines:
            continue
        used_images.add(index)
        used_headlines.add(text.casefold())
        chosen.append((index, text))
        if len(chosen) == COVER_COUNT:
            break
    fallback = fallback_headline(first_sentence)
    spare = [i for i in range(candidate_count) if i not in used_images]
    while len(chosen) < min(COVER_COUNT, candidate_count) and spare:
        headline = fallback if fallback.casefold() not in used_headlines else chosen[0][1]
        used_headlines.add(headline.casefold())
        chosen.append((spare.pop(len(spare) // 2), headline))
    return [PlannedCover(n, index, text) for n, (index, text) in enumerate(chosen, start=1)]
