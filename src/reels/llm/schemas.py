"""Response schemas for Structured Outputs (all fields required, no defaults)."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel


class SegmentHighlight(BaseModel):
    segment: int
    kind: Literal["structure", "key", "none"]
    first_word_id: int | None
    last_word_id: int | None
    reason: str


class HighlightPlan(BaseModel):
    segments: list[SegmentHighlight]
