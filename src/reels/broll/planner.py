"""B-roll planning: the LLM suggests spots, the code validates them and applies the rules."""

from __future__ import annotations

from collections.abc import Collection, Sequence
from typing import Literal

from pydantic import BaseModel

from reels.domain.broll_rules import BrollRules, Candidate, Slot, select, word_span
from reels.domain.timeline import Timeline
from reels.domain.words import Word
from reels.llm.schemas import BrollSuggestion

MAX_SUGGESTIONS = 8
MAX_CARD_WORDS = 6


class PlannedSpot(BaseModel):
    """A validated suggestion (anchored to words) – the input for placements."""

    first_word: int
    last_word: int
    kind: Literal["stock", "textcard"]
    idea: str
    query: str | None
    card_text: str | None
    priority: int


def validate(suggestions: Sequence[BrollSuggestion], kept: Collection[int]) -> list[PlannedSpot]:
    """Drop suggestions with unknown/cut words, missing query (stock) or text (card)."""
    spots: list[PlannedSpot] = []
    for s in suggestions[:MAX_SUGGESTIONS]:
        idea = s.idea.strip()
        query = s.query.strip() or None
        card = " ".join((s.card_text or "").split()) or None
        if s.first_word_id > s.last_word_id or not idea:
            continue
        if s.first_word_id not in kept or s.last_word_id not in kept:
            continue
        if s.kind == "stock" and not query:
            continue
        if s.kind == "textcard" and (not card or len(card.split()) > MAX_CARD_WORDS):
            continue
        spots.append(
            PlannedSpot(
                first_word=s.first_word_id,
                last_word=s.last_word_id,
                kind=s.kind,
                idea=idea,
                query=query if s.kind == "stock" else None,
                card_text=card if s.kind == "textcard" else None,
                priority=min(max(s.priority, 1), 3),
            )
        )
    return spots


def place(
    spots: Sequence[PlannedSpot], words: Sequence[Word], timeline: Timeline, rules: BrollRules
) -> list[tuple[PlannedSpot, Slot]]:
    """Apply the rules on the output timeline; returns the chosen spots in time order."""
    candidates = []
    for i, spot in enumerate(spots):
        span = word_span(words, spot.first_word, spot.last_word, timeline)
        if span is not None:
            candidates.append(Candidate(key=i, start=span[0], end=span[1], priority=spot.priority))
    return [(spots[slot.key], slot) for slot in select(candidates, timeline.duration, rules)]
