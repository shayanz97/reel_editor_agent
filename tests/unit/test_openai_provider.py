"""OpenAI provider against a fake client (no network)."""

from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from pydantic import BaseModel, ValidationError

from reels.config import Settings
from reels.llm.base import LLMError
from reels.llm.openai_provider import OpenAIProvider
from reels.llm.schemas import HighlightPlan, SegmentHighlight


class _Responses:
    def __init__(self, outcomes: list[Any]) -> None:
        self.outcomes = outcomes
        self.calls: list[dict[str, Any]] = []

    def parse(self, **kwargs: Any) -> Any:
        self.calls.append(kwargs)
        outcome = self.outcomes.pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        return SimpleNamespace(
            output_parsed=outcome, usage=SimpleNamespace(input_tokens=100, output_tokens=20)
        )


def _provider(outcomes: list[Any]) -> tuple[OpenAIProvider, _Responses]:
    responses = _Responses(outcomes)
    client = SimpleNamespace(responses=responses)
    return OpenAIProvider(
        "k", "text-model", "vision-model", "embed-model", client=client
    ), responses


def _validation_error() -> ValidationError:
    class M(BaseModel):
        x: int

    try:
        M.model_validate({"x": "no"})
    except ValidationError as exc:
        return exc
    raise AssertionError


PLAN = HighlightPlan(
    segments=[SegmentHighlight(segment=1, kind="key", first_word_id=3, last_word_id=3, reason="x")]
)


def test_structured_returns_parsed_and_records_usage() -> None:
    provider, responses = _provider([PLAN])
    assert provider.structured("highlights", "prompt", HighlightPlan) == PLAN
    call = responses.calls[0]
    assert call["model"] == "text-model"
    assert call["text_format"] is HighlightPlan
    assert call["input"][0]["content"] == [{"type": "input_text", "text": "prompt"}]
    (usage,) = provider.usage
    assert (usage.task, usage.model, usage.input_tokens, usage.output_tokens) == (
        "highlights", "text-model", 100, 20,
    )  # fmt: skip


def test_retries_once_after_validation_error() -> None:
    provider, responses = _provider([_validation_error(), PLAN])
    assert provider.structured("highlights", "p", HighlightPlan) == PLAN
    assert len(responses.calls) == 2


def test_fails_after_two_bad_answers() -> None:
    provider, _ = _provider([_validation_error(), None])
    with pytest.raises(LLMError, match="2 attempts"):
        provider.structured("highlights", "p", HighlightPlan)


def test_images_use_vision_model(tmp_path: Path) -> None:
    img = tmp_path / "frame.png"
    img.write_bytes(b"\x89PNG fake")
    provider, responses = _provider([PLAN])
    provider.structured("cover", "p", HighlightPlan, images=[img])
    call = responses.calls[0]
    assert call["model"] == "vision-model"
    assert call["input"][0]["content"][1]["image_url"].startswith("data:image/png;base64,")


def test_from_settings_requires_key_and_model() -> None:
    assert OpenAIProvider.from_settings(Settings(_env_file=None)) is None  # type: ignore[call-arg]
    no_model = Settings(_env_file=None, openai_api_key="k")  # type: ignore[call-arg]
    assert OpenAIProvider.from_settings(no_model) is None
