"""Vocabulary check after transcription: questions about words that are probably misheard."""

from __future__ import annotations

from collections.abc import Collection, Sequence

from pydantic import BaseModel

from reels.domain.glossary import GlossaryTerm
from reels.domain.segments import Segment
from reels.domain.words import TextOverlay, Word, display_text, normalize_token

MAX_QUESTIONS = 5
MAX_WORDS = 3


class SuspiciousWord(BaseModel):
    """One suggestion (from the LLM) before validation."""

    segment: int
    first_word: int
    last_word: int
    suggestion: str | None


class VocabQuestion(BaseModel):
    id: int
    segment: int
    word_ids: list[int]
    text: str  # as transcribed (with corrections already applied)
    context: str  # the whole segment, so the client can judge
    suggestion: str | None
    answer: str | None = None  # "keep" or the correct spelling
    message_id: int | None = None


def known_tokens(terms: Sequence[GlossaryTerm]) -> set[str]:
    """Normalized terms and variants of the glossary (confirmed and suggested)."""
    known: set[str] = set()
    for t in terms:
        for text in (t.term, *t.variants):
            known.add(" ".join(normalize_token(p) for p in text.split() if normalize_token(p)))
    return known


def select_questions(
    items: Sequence[SuspiciousWord],
    words: Sequence[Word],
    segments: Sequence[Segment],
    overlay: TextOverlay,
    known: Collection[str],
    max_questions: int = MAX_QUESTIONS,
) -> list[VocabQuestion]:
    """Validate suggestions: inside their segment, at most MAX_WORDS, visible, not already in the
    glossary, each spelling asked once. Invalid items are dropped."""
    by_id = {w.id: w for w in words}
    by_number = {s.number: s for s in segments}
    asked: set[str] = set()
    questions: list[VocabQuestion] = []
    for item in items:
        segment = by_number.get(item.segment)
        if segment is None or item.first_word > item.last_word:
            continue
        ids = list(range(item.first_word, item.last_word + 1))
        if (
            item.first_word < segment.first_word
            or item.last_word > segment.last_word
            or len(ids) > MAX_WORDS
        ):
            continue
        shown = [display_text(by_id[i], overlay) for i in ids if i in by_id]
        text = " ".join(t for t in shown if t.strip())
        norm = " ".join(normalize_token(p) for p in text.split() if normalize_token(p))
        if not norm or norm in known or norm in asked:
            continue
        suggestion = (item.suggestion or "").strip() or None
        if suggestion is not None and normalize_token(suggestion) == norm:
            suggestion = None
        asked.add(norm)
        questions.append(
            VocabQuestion(
                id=len(questions) + 1,
                segment=segment.number,
                word_ids=ids,
                text=text,
                context=segment.text,
                suggestion=suggestion,
            )
        )
        if len(questions) >= max_questions:
            break
    return questions


def transcript_lines(
    segments: Sequence[Segment], words: Sequence[Word], overlay: TextOverlay
) -> str:
    """`<segment>: [id]word …` for the prompt."""
    by_id = {w.id: w for w in words}
    lines = []
    for seg in segments:
        tokens = [
            f"[{i}]{display_text(by_id[i], overlay)}"
            for i in range(seg.first_word, seg.last_word + 1)
            if i in by_id and display_text(by_id[i], overlay).strip()
        ]
        lines.append(f"{seg.number}: " + " ".join(tokens))
    return "\n".join(lines)
