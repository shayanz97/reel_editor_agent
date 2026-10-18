"""6.7: does a candidate image fit the planned idea? (vision model, single preview image)."""

from __future__ import annotations

from pathlib import Path

from reels.llm.base import LLMError, LLMProvider, load_prompt
from reels.llm.schemas import AssetCheck
from reels.log import get_logger

log = get_logger(__name__)


def check_image(llm: LLMProvider, image: Path, idea: str, context: str) -> int:
    """Score 0..10; an LLM error counts as 0 (rather skip than show something unchecked)."""
    prompt = load_prompt("broll_check", idea=idea, context=context)
    try:
        result = llm.structured("broll_check", prompt, AssetCheck, images=[image])
    except LLMError as exc:
        log.warning("broll_check_failed", extra={"error": str(exc)})
        return 0
    return max(0, min(10, result.score))
