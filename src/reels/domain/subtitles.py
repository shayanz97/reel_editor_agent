"""Subtitle groups on the OUTPUT timeline, built from word ids."""

from __future__ import annotations

from collections.abc import Collection, Sequence

from pydantic import BaseModel

from reels.domain.segments import CLAUSE_END, Span
from reels.domain.timeline import Timeline
from reels.domain.words import TextOverlay, Word, display_text


class SubWord(BaseModel):
    word_id: int
    text: str
    start: float  # output timeline
    end: float
    highlight: bool = False


class SubtitleEvent(BaseModel):
    start: float  # output timeline
    end: float
    words: list[SubWord]

    @property
    def text(self) -> str:
        return " ".join(w.text for w in self.words)


class GroupParams(BaseModel):
    max_words: int = 4
    # Keeps a group on one line at the default font size (1080 px wide frame).
    max_chars: int = 22
    # A group stays visible a little after its last word, unless the next group starts.
    linger_s: float = 0.25


def build_events(
    words: Sequence[Word],
    timeline: Timeline,
    spans: Sequence[Span],
    params: GroupParams,
    overlay: TextOverlay | None = None,
    highlights: Collection[int] = (),
) -> list[SubtitleEvent]:
    """Group kept, visible words into subtitle events.

    A new group starts when the group is full (`max_words`), when the next word would push
    it over `max_chars`, at segment (sentence) boundaries and after clause punctuation.
    """
    span_of = {i: n for n, s in enumerate(spans) for i in range(s.first_word, s.last_word + 1)}
    groups: list[list[SubWord]] = []
    current: list[SubWord] = []
    current_span: int | None = None

    for w in words:
        out = timeline.word_to_output(w)
        text = display_text(w, overlay).strip()
        if out is None or not text:
            continue
        sub = SubWord(
            word_id=w.id, text=text, start=out[0], end=out[1], highlight=w.id in highlights
        )
        span = span_of.get(w.id)
        if current and _must_break(current, sub, span != current_span, params):
            groups.append(current)
            current = []
        current.append(sub)
        current_span = span
    if current:
        groups.append(current)

    events: list[SubtitleEvent] = []
    for i, group in enumerate(groups):
        end = group[-1].end + params.linger_s
        if i + 1 < len(groups):
            end = min(end, groups[i + 1][0].start)
        events.append(SubtitleEvent(start=group[0].start, end=max(end, group[-1].end), words=group))
    return events


def _must_break(group: list[SubWord], nxt: SubWord, new_span: bool, params: GroupParams) -> bool:
    if new_span or len(group) >= params.max_words:
        return True
    if group[-1].text.endswith(CLAUSE_END):
        return True
    chars = sum(len(w.text) for w in group) + len(group) + len(nxt.text)
    return chars > params.max_chars
