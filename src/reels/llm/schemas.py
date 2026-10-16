"""Response schemas for Structured Outputs (all fields required, no defaults)."""

from __future__ import annotations

from pydantic import BaseModel


class HighlightPick(BaseModel):
    word_id: int
    reason: str


class HighlightPlan(BaseModel):
    highlights: list[HighlightPick]
