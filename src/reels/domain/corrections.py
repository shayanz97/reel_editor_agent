"""Corrections: allowlisted operations on a version, validated against what the client saw.

Free text -> LLM -> CorrectionPlan -> `resolve` (validation against the base version) ->
client confirms -> `apply` -> state of the new version -> rerun from `rerun_step`.

Operations refer to segment numbers of the base version and to words by their text (`find`).
Nothing is guessed: a segment that does not exist or a `find` that is missing or ambiguous
becomes an Issue, which the bot turns into a question for the client.
"""

from __future__ import annotations

import re
from collections.abc import Collection, Sequence
from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from reels.domain.segments import Segment, Span
from reels.domain.words import TextOverlay, Word, display_text, normalize_token

SubtitlePosition = Literal["lower_third", "center", "upper_third"]


class _Op(BaseModel):
    model_config = ConfigDict(frozen=True)


class ReplaceText(_Op):
    op: Literal["replace_text"]
    segment: int
    find: str
    replace: str


class DeleteSegment(_Op):
    op: Literal["delete_segment"]
    segment: int


class RestoreSegment(_Op):
    op: Literal["restore_segment"]
    removed: int  # number of the removed segment as listed (E1, E2 …)


class DeleteWords(_Op):
    op: Literal["delete_words"]
    segment: int
    find: str


class KeepPause(_Op):
    op: Literal["keep_pause"]
    segment: int  # keep the pause AFTER this segment at its original length


class Highlight(_Op):
    op: Literal["highlight"]
    segment: int
    find: str


class Unhighlight(_Op):
    op: Literal["unhighlight"]
    segment: int
    find: str | None  # None: all highlights in the segment


class MoveSubtitles(_Op):
    op: Literal["move_subtitles"]
    position: SubtitlePosition


class SplitSegment(_Op):
    op: Literal["split_segment"]
    segment: int
    after: str  # split after these words


class Note(_Op):
    op: Literal["note"]
    text: str  # forwarded to the team, changes nothing in the video


AnyOperation = (
    ReplaceText
    | DeleteSegment
    | RestoreSegment
    | DeleteWords
    | KeepPause
    | Highlight
    | Unhighlight
    | MoveSubtitles
    | SplitSegment
    | Note
)
Operation = Annotated[AnyOperation, Field(discriminator="op")]


class CorrectionPlan(BaseModel):
    operations: list[Operation]
    clarification: str | None = None  # question back to the client when the request is unclear


# Earliest pipeline step an operation needs to rerun. Transcription never reruns.
RERUN_STEP: dict[str, str | None] = {
    "replace_text": "segments",
    "split_segment": "segments",
    "delete_segment": "cutplan",
    "restore_segment": "cutplan",
    "delete_words": "cutplan",
    "keep_pause": "cutplan",
    "highlight": "subtitles",
    "unhighlight": "subtitles",
    "move_subtitles": "subtitles",
    "note": None,
}
# Segments are always recomputed: cheap, and the preview's sentence list comes from them.
MIN_RERUN_STEP = "segments"


def rerun_step(ops: Sequence[AnyOperation], step_order: Sequence[str]) -> str | None:
    """Earliest step to rerun for these operations, None if nothing in the video changes."""
    steps = [s for s in (RERUN_STEP[op.op] for op in ops) if s is not None]
    if not steps:
        return None
    return min([*steps, MIN_RERUN_STEP], key=step_order.index)


# --- version state ------------------------------------------------------------------


class VersionState(BaseModel):
    """Everything a correction can change. Stored per version (text_overlay, highlights, edits)."""

    overlay: dict[int, str] = Field(default_factory=dict)
    highlights: set[int] = Field(default_factory=set)
    removed_words: set[int] = Field(default_factory=set)
    keep_pauses_after: set[int] = Field(default_factory=set)
    splits_after: set[int] = Field(default_factory=set)
    subtitle: dict[str, Any] = Field(default_factory=dict)  # overrides of SubtitleSettings

    def edits(self) -> dict[str, Any]:
        """JSON for versions.edits."""
        return {
            "removed_words": sorted(self.removed_words),
            "keep_pauses_after": sorted(self.keep_pauses_after),
            "splits_after": sorted(self.splits_after),
            "subtitle": dict(self.subtitle),
        }

    @classmethod
    def from_version(
        cls, text_overlay: dict[str, str], highlights: Sequence[int], edits: dict[str, Any]
    ) -> VersionState:
        return cls(
            overlay={int(k): v for k, v in text_overlay.items()},
            highlights=set(highlights),
            removed_words=set(edits.get("removed_words", [])),
            keep_pauses_after=set(edits.get("keep_pauses_after", [])),
            splits_after=set(edits.get("splits_after", [])),
            subtitle=dict(edits.get("subtitle", {})),
        )


def removed_segments(spans: Sequence[Span], removed_words: Collection[int]) -> list[Span]:
    """Spans removed completely by corrections, listed for restore_segment (E1, E2 …)."""
    return [
        s for s in spans if all(i in removed_words for i in range(s.first_word, s.last_word + 1))
    ]


# --- validation -------------------------------------------------------------------

IssueKind = Literal[
    "segment_missing",
    "find_missing",
    "find_ambiguous",
    "removed_missing",
    "split_at_end",
    "nothing_to_do",
]


class Issue(BaseModel):
    kind: IssueKind
    segment: int | None = None
    find: str | None = None


class Resolved(BaseModel):
    """An operation plus the word ids it acts on (resolved against the base version)."""

    op: Operation
    word_ids: list[int] = Field(default_factory=list)


def _find(
    words: Sequence[Word], segment: Segment, text: str, overlay: TextOverlay
) -> list[list[int]]:
    """All occurrences of `text` (word-boundary safe, case-insensitive) among the visible words.

    Each occurrence is the list of word ids from its first to its last matched word,
    including hidden words in between.
    """
    tokens = [t for t in (normalize_token(p) for p in text.split()) if t]
    if not tokens:
        return []
    by_id = {w.id: w for w in words}
    visible = [
        by_id[i]
        for i in range(segment.first_word, segment.last_word + 1)
        if i in by_id and display_text(by_id[i], overlay).strip()
    ]
    norm = [normalize_token(display_text(w, overlay)) for w in visible]
    hits: list[list[int]] = []
    for start in range(len(visible) - len(tokens) + 1):
        if norm[start : start + len(tokens)] == tokens:
            first, last = visible[start].id, visible[start + len(tokens) - 1].id
            hits.append(list(range(first, last + 1)))
    return hits


def resolve(
    plan_ops: Sequence[AnyOperation],
    words: Sequence[Word],
    segments: Sequence[Segment],
    removed: Sequence[Span],
    state: VersionState,
) -> tuple[list[Resolved], list[Issue]]:
    """Validate operations against the base version; collect issues instead of guessing."""
    by_number = {s.number: s for s in segments}
    resolved: list[Resolved] = []
    issues: list[Issue] = []

    for op in plan_ops:
        if isinstance(op, (MoveSubtitles, Note)):
            resolved.append(Resolved(op=op))
            continue
        if isinstance(op, RestoreSegment):
            if not 1 <= op.removed <= len(removed):
                issues.append(Issue(kind="removed_missing", segment=op.removed))
                continue
            span = removed[op.removed - 1]
            resolved.append(
                Resolved(op=op, word_ids=list(range(span.first_word, span.last_word + 1)))
            )
            continue

        segment = by_number.get(op.segment)
        if segment is None:
            issues.append(Issue(kind="segment_missing", segment=op.segment))
            continue
        segment_ids = list(range(segment.first_word, segment.last_word + 1))

        if isinstance(op, (DeleteSegment, KeepPause)):
            resolved.append(Resolved(op=op, word_ids=segment_ids))
            continue
        if isinstance(op, Unhighlight) and op.find is None:
            resolved.append(Resolved(op=op, word_ids=segment_ids))
            continue

        find = op.after if isinstance(op, SplitSegment) else op.find
        assert find is not None
        hits = _find(words, segment, find, state.overlay)
        if not hits:
            issues.append(Issue(kind="find_missing", segment=op.segment, find=find))
            continue
        if len(hits) > 1:
            issues.append(Issue(kind="find_ambiguous", segment=op.segment, find=find))
            continue
        ids = hits[0]
        if isinstance(op, SplitSegment) and ids[-1] >= segment.last_word:
            issues.append(Issue(kind="split_at_end", segment=op.segment, find=find))
            continue
        resolved.append(Resolved(op=op, word_ids=ids))

    if not plan_ops and not issues:
        issues.append(Issue(kind="nothing_to_do"))
    return resolved, issues


# --- application ------------------------------------------------------------------


def _replace_span(overlay: dict[int, str], ids: Sequence[int], current: Sequence[str],
                  replacement: str) -> None:  # fmt: skip
    """Replacement goes to the first word (keeping outer punctuation), the rest is hidden."""
    lead = re.match(r"^[^\w]*", current[0])
    trail = re.search(r"[^\w]*$", current[-1])
    text = (lead.group() if lead else "") + replacement.strip() + (trail.group() if trail else "")
    for k, word_id in enumerate(ids):
        overlay[word_id] = text if k == 0 else ""


def apply(resolved: Sequence[Resolved], words: Sequence[Word], state: VersionState) -> VersionState:
    """New version state. The base state is not modified."""
    new = state.model_copy(deep=True)
    by_id = {w.id: w for w in words}
    for r in resolved:
        op = r.op
        if isinstance(op, ReplaceText):
            current = [display_text(by_id[i], new.overlay) for i in r.word_ids]
            visible = [t for t in current if t.strip()]
            _replace_span(new.overlay, r.word_ids, visible or current, op.replace)
            new.highlights -= set(r.word_ids[1:])
        elif isinstance(op, (DeleteSegment, DeleteWords)):
            new.removed_words |= set(r.word_ids)
        elif isinstance(op, RestoreSegment):
            new.removed_words -= set(r.word_ids)
        elif isinstance(op, KeepPause):
            new.keep_pauses_after.add(r.word_ids[-1])
        elif isinstance(op, Highlight):
            new.highlights |= {i for i in r.word_ids if display_text(by_id[i], new.overlay).strip()}
        elif isinstance(op, Unhighlight):
            new.highlights -= set(r.word_ids)
        elif isinstance(op, MoveSubtitles):
            new.subtitle["position"] = op.position
        elif isinstance(op, SplitSegment):
            new.splits_after.add(r.word_ids[-1])
    return new
