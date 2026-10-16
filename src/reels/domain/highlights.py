"""Validation of highlight suggestions (from the LLM) against the current version."""

from __future__ import annotations

from collections.abc import Collection, Iterable, Sequence

from reels.domain.segments import Segment
from reels.domain.words import TextOverlay, Word, display_text


def transcript_for_prompt(
    words: Sequence[Word], segments: Sequence[Segment], overlay: TextOverlay | None = None
) -> str:
    """One segment per line, words as `[id]text`; hidden words are left out."""
    by_id = {w.id: w for w in words}
    lines: list[str] = []
    for seg in segments:
        tokens = [
            f"[{i}]{display_text(by_id[i], overlay)}"
            for i in range(seg.first_word, seg.last_word + 1)
            if i in by_id and display_text(by_id[i], overlay).strip()
        ]
        lines.append(" ".join(tokens))
    return "\n".join(lines)


def select_highlights(
    suggested: Iterable[int],
    segments: Sequence[Segment],
    allowed: Collection[int],
    max_per_segment: int = 1,
) -> list[int]:
    """Keep only allowed (kept, visible) ids, drop duplicates, cap per segment. Never guesses."""
    per_segment: dict[int, int] = {}
    seen: set[int] = set()
    result: list[int] = []
    for word_id in suggested:
        if word_id in seen or word_id not in allowed:
            continue
        seg = next((s for s in segments if s.first_word <= word_id <= s.last_word), None)
        if seg is None or per_segment.get(seg.number, 0) >= max_per_segment:
            continue
        per_segment[seg.number] = per_segment.get(seg.number, 0) + 1
        seen.add(word_id)
        result.append(word_id)
    return sorted(result)
