"""Cut plan: which parts of the original to keep. Lives on the ORIGINAL timeline.

Rules (CLAUDE.md §5):
- Never cut inside a word (kept or removed).
- Pauses longer than `pause_threshold_s` are shortened, not removed entirely.
- Every cut gets a buffer (`cut_padding_s`) around the speech.
- Fillers are only removed if the transcript contains them.
"""

from __future__ import annotations

import itertools
from collections.abc import Iterable, Sequence

from pydantic import BaseModel

from reels.domain.words import Word, normalize_token

Span = tuple[float, float]

# Hesitation sounds only. "eh" is deliberately missing: in German it is also a word ("eh klar").
FILLERS = frozenset({"äh", "ähm", "öh", "öhm", "ehm", "hm", "hmm"})


class KeepRange(BaseModel):
    start: float  # original timeline, including buffer
    end: float
    first_word: int | None
    last_word: int | None

    @property
    def duration(self) -> float:
        return self.end - self.start


class CutPlan(BaseModel):
    keep: list[KeepRange]  # sorted, non-overlapping
    params: dict[str, float]  # thresholds used, for traceability

    @property
    def output_duration(self) -> float:
        return sum(r.duration for r in self.keep)

    @property
    def cut_count(self) -> int:
        """Number of joins in the output (leading/trailing trims are not counted)."""
        return max(len(self.keep) - 1, 0)


class CutParams(BaseModel):
    pause_threshold_s: float = 0.4
    pause_target_s: float = 0.15
    cut_padding_s: float = 0.08

    @property
    def edge_padding_s(self) -> float:
        """Silence kept on each side of a cut. Two of them form the shortened pause."""
        return max(self.cut_padding_s, self.pause_target_s / 2)


def filler_ids(words: Iterable[Word]) -> set[int]:
    return {w.id for w in words if normalize_token(w.text) in FILLERS}


def build_cut_plan(
    words: Sequence[Word],
    speech: Sequence[Span],
    duration: float,
    params: CutParams,
    removed: Iterable[int] = (),
    keep_pauses_after: Iterable[int] = (),
) -> CutPlan:
    """Build the cut plan from word timings and VAD speech regions.

    - Content = kept words + VAD speech (VAD protects word tails the aligner cut short).
      Removed words are subtracted from the VAD regions; scraps shorter than the cut
      padding are dropped.
    - Content closer than `pause_threshold_s` is merged unless a removed word lies between.
    - Clusters without any kept word (noise, unrecognised sounds) are dropped.
    - Each cluster is padded by `edge_padding_s`, clamped to the video and to word edges.
    - The pause after a word in `keep_pauses_after` is kept at full length (correction
      `keep_pause`), unless a removed word lies in it.
    """
    removed_ids = set(removed)
    kept = [w for w in words if w.id not in removed_ids]
    dropped = [w for w in words if w.id in removed_ids]
    record = {
        "pause_threshold_s": params.pause_threshold_s,
        "pause_target_s": params.pause_target_s,
        "cut_padding_s": params.cut_padding_s,
        "edge_padding_s": params.edge_padding_s,
        "removed_words": float(len(dropped)),
        "kept_pauses": float(len(set(keep_pauses_after))),
    }
    if not kept:
        return CutPlan(keep=[], params=record)

    dropped_spans = [(w.start, w.end) for w in dropped]
    content: list[Span] = [(w.start, w.end) for w in kept]
    keep_after = set(keep_pauses_after)
    for current, following in itertools.pairwise(kept):
        if current.id in keep_after and following.start > current.end:
            content.append((current.end, following.start))
    for region in speech:
        for piece in _subtract(region, dropped_spans):
            if piece[1] - piece[0] >= params.cut_padding_s:
                content.append(piece)

    clusters = _cluster(sorted(content), dropped_spans, params.pause_threshold_s)
    clusters = [c for c in clusters if any(_overlaps((w.start, w.end), c) for w in kept)]

    pad = params.edge_padding_s
    ranges: list[KeepRange] = []
    for c_start, c_end in clusters:
        inside = [w for w in kept if _within((w.start, w.end), (c_start, c_end))]
        outside = [w for w in words if w not in inside]
        start = _clamp_start(max(c_start - pad, 0.0), c_start, outside)
        end = _clamp_end(min(c_end + pad, duration), c_end, outside)
        ranges.append(
            KeepRange(
                start=round(start, 3),
                end=round(end, 3),
                first_word=min((w.id for w in inside), default=None),
                last_word=max((w.id for w in inside), default=None),
            )
        )
    return CutPlan(keep=_merge_touching(ranges), params=record)


def _subtract(span: Span, holes: Sequence[Span]) -> list[Span]:
    pieces = [span]
    for h_start, h_end in holes:
        nxt: list[Span] = []
        for p_start, p_end in pieces:
            if h_end <= p_start or h_start >= p_end:
                nxt.append((p_start, p_end))
                continue
            if h_start > p_start:
                nxt.append((p_start, h_start))
            if h_end < p_end:
                nxt.append((h_end, p_end))
        pieces = nxt
    return pieces


def _cluster(content: Sequence[Span], barriers: Sequence[Span], threshold: float) -> list[Span]:
    clusters: list[Span] = []
    for start, end in content:
        if clusters:
            c_start, c_end = clusters[-1]
            gap = (c_end, start)
            blocked = any(_overlaps(b, gap) for b in barriers)
            if start - c_end <= threshold and not blocked:
                clusters[-1] = (c_start, max(c_end, end))
                continue
        clusters.append((start, end))
    return clusters


def _overlaps(a: Span, b: Span) -> bool:
    return a[0] < b[1] and a[1] > b[0]


def _within(inner: Span, outer: Span) -> bool:
    return inner[0] >= outer[0] and inner[1] <= outer[1]


def _clamp_start(start: float, content_start: float, others: Sequence[Word]) -> float:
    """Move the start forward out of any word that is not part of this range."""
    for w in others:
        if w.start < content_start and w.start < start < w.end:
            start = w.end
    return min(start, content_start)


def _clamp_end(end: float, content_end: float, others: Sequence[Word]) -> float:
    """Move the end back out of any word that is not part of this range."""
    for w in others:
        if w.end > content_end and w.start < end < w.end:
            end = w.start
    return max(end, content_end)


def _merge_touching(ranges: Sequence[KeepRange]) -> list[KeepRange]:
    merged: list[KeepRange] = []
    for r in ranges:
        if merged and r.start <= merged[-1].end:
            prev = merged[-1]
            ids = [
                i
                for i in (prev.first_word, prev.last_word, r.first_word, r.last_word)
                if i is not None
            ]
            merged[-1] = KeepRange(
                start=prev.start,
                end=max(prev.end, r.end),
                first_word=min(ids, default=None),
                last_word=max(ids, default=None),
            )
        else:
            merged.append(r)
    return merged
