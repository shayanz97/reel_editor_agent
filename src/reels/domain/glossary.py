"""Client glossary: transcription hint and deterministic variant replacement."""

from __future__ import annotations

import re
from collections import Counter
from collections.abc import Sequence
from typing import Literal

from pydantic import BaseModel, Field

from reels.domain.words import TextOverlay, Word, normalize_token

GlossaryStatus = Literal["confirmed", "suggested"]

# Whisper only keeps the last ~224 tokens of the initial prompt; stay well below that.
DEFAULT_HINT_MAX_CHARS = 400


class GlossaryTerm(BaseModel):
    term: str
    variants: list[str] = Field(default_factory=list)
    category: str | None = None
    note: str | None = None
    highlight: bool = False
    source: str = "manual"
    status: GlossaryStatus = "confirmed"
    hits: int = 0


def build_prompt_hint(
    terms: Sequence[GlossaryTerm], max_chars: int = DEFAULT_HINT_MAX_CHARS
) -> str | None:
    """Comma-separated list of the most relevant confirmed terms, limited to `max_chars`.

    Relevance: most hits first, then highlighted terms, then alphabetical (deterministic).
    """
    confirmed = [t for t in terms if t.status == "confirmed"]
    ranked = sorted(confirmed, key=lambda t: (-t.hits, not t.highlight, t.term.casefold()))
    prefix = "Fachbegriffe: "
    picked: list[str] = []
    length = len(prefix) + 1  # trailing "."
    for t in ranked:
        added = len(t.term) + (2 if picked else 0)
        if length + added > max_chars:
            continue
        picked.append(t.term)
        length += added
    if not picked:
        return None
    return prefix + ", ".join(picked) + "."


class _Pattern(BaseModel):
    tokens: tuple[str, ...]
    term: str


def _patterns(terms: Sequence[GlossaryTerm]) -> list[_Pattern]:
    patterns: list[_Pattern] = []
    for t in terms:
        if t.status != "confirmed":
            continue
        # The term itself is an implicit variant: fixes case ("kubernetes" -> "Kubernetes").
        for variant in [t.term, *t.variants]:
            tokens = tuple(normalize_token(p) for p in variant.split())
            tokens = tuple(tok for tok in tokens if tok)
            if tokens:
                patterns.append(_Pattern(tokens=tokens, term=t.term))
    # Longest match wins; ties resolved by order of definition.
    patterns.sort(key=lambda p: -len(p.tokens))
    return patterns


_LEADING = re.compile(r"^[^\w]*", re.UNICODE)
_TRAILING = re.compile(r"[^\w]*$", re.UNICODE)


def apply_glossary(
    words: Sequence[Word],
    terms: Sequence[GlossaryTerm],
    overlay: TextOverlay | None = None,
) -> tuple[dict[int, str], Counter[str]]:
    """Replace known variants word-boundary safe, case-insensitive and across several words.

    Returns a new text overlay (existing overlay entries are kept unless replaced) and the
    number of occurrences per term, already correct or replaced (for the `hits` counter,
    which ranks terms for the transcription hint). The first word of a
    multi-word match gets the term, the others become empty (hidden in subtitles, still timed).
    Leading punctuation of the first and trailing punctuation of the last word are preserved.
    """
    result: dict[int, str] = dict(overlay or {})
    hits: Counter[str] = Counter()
    patterns = _patterns(terms)
    texts = [result.get(w.id, w.text) for w in words]
    tokens = [normalize_token(t) for t in texts]

    i = 0
    while i < len(words):
        match = next(
            (p for p in patterns if tuple(tokens[i : i + len(p.tokens)]) == p.tokens), None
        )
        if match is None:
            i += 1
            continue
        n = len(match.tokens)
        lead = _LEADING.match(texts[i])
        trail = _TRAILING.search(texts[i + n - 1])
        new_first = (lead.group() if lead else "") + match.term + (trail.group() if trail else "")
        new_texts = [new_first, *([""] * (n - 1))]
        hits[match.term] += 1
        for k, text in enumerate(new_texts):
            if text != texts[i + k]:
                result[words[i + k].id] = text
        i += n
    return result, hits
