"""Provider-neutral LLM interface. Every answer is validated against a Pydantic schema."""

from __future__ import annotations

from dataclasses import dataclass
from importlib import resources
from pathlib import Path
from string import Template
from typing import Protocol, TypeVar

from pydantic import BaseModel

T = TypeVar("T", bound=BaseModel)


class LLMError(RuntimeError):
    pass


@dataclass(frozen=True)
class LLMUsage:
    """Token usage of one call, collected for jobs.metrics (cost control)."""

    task: str
    model: str
    input_tokens: int
    output_tokens: int


class LLMProvider(Protocol):
    usage: list[LLMUsage]

    def structured(
        self, task: str, prompt: str, schema: type[T], images: list[Path] | None = None
    ) -> T: ...

    def embed(self, texts: list[str]) -> list[list[float]]: ...


def load_prompt(name: str, **values: str) -> str:
    """Load `llm/prompts/<name>.md` and fill `$placeholders` (all must be given)."""
    text = resources.files("reels.llm.prompts").joinpath(f"{name}.md").read_text(encoding="utf-8")
    return Template(text).substitute(values)
