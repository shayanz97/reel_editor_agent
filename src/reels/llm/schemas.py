"""Response schemas for Structured Outputs (all fields required, no defaults)."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel

from reels.domain.corrections import AnyOperation


class SegmentHighlight(BaseModel):
    segment: int
    kind: Literal["structure", "key", "none"]
    first_word_id: int | None
    last_word_id: int | None
    reason: str


class HighlightPlan(BaseModel):
    segments: list[SegmentHighlight]


# Plain union of the operation models: a discriminated union would produce JSON Schema
# `oneOf`, which Structured Outputs in strict mode does not accept. Docstrings of these
# models end up in the schema sent to the API, so comments are used instead.
class CorrectionResponse(BaseModel):
    operations: list[AnyOperation]
    clarification: str | None


class SuspiciousWordItem(BaseModel):
    segment: int
    first_word_id: int
    last_word_id: int
    suggestion: str | None
    reason: str


class VocabResponse(BaseModel):
    items: list[SuspiciousWordItem]
