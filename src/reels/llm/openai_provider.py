"""OpenAI implementation of LLMProvider (Responses API + Structured Outputs)."""

from __future__ import annotations

import base64
import mimetypes
from pathlib import Path
from typing import Any

from pydantic import ValidationError

from reels.config import Settings
from reels.llm.base import LLMError, LLMUsage, T
from reels.log import get_logger

log = get_logger(__name__)

MAX_ATTEMPTS = 2  # one retry after a validation failure, then report the error


class OpenAIProvider:
    def __init__(
        self,
        api_key: str,
        model_text: str | None,
        model_vision: str | None = None,
        model_embed: str | None = None,
        client: Any = None,
    ) -> None:
        if client is None:
            from openai import OpenAI

            client = OpenAI(api_key=api_key)
        self.client = client
        self.model_text = model_text
        self.model_vision = model_vision
        self.model_embed = model_embed
        self.usage: list[LLMUsage] = []

    @classmethod
    def from_settings(cls, s: Settings) -> OpenAIProvider | None:
        """Provider if key and text model are configured, else None (callers fall back)."""
        if s.openai_api_key is None or not s.openai_model_text:
            return None
        return cls(
            s.openai_api_key.get_secret_value(),
            s.openai_model_text,
            s.openai_model_vision,
            s.openai_model_embed,
        )

    def structured(
        self, task: str, prompt: str, schema: type[T], images: list[Path] | None = None
    ) -> T:
        model = self.model_vision if images else self.model_text
        if not model:
            raise LLMError(f"no OpenAI model configured for task {task!r}")
        content: list[dict[str, Any]] = [{"type": "input_text", "text": prompt}]
        for image in images or []:
            content.append({"type": "input_image", "image_url": _data_url(image)})

        last_error: Exception | None = None
        for attempt in range(1, MAX_ATTEMPTS + 1):
            try:
                response = self.client.responses.parse(
                    model=model,
                    input=[{"role": "user", "content": content}],
                    text_format=schema,
                )
            except ValidationError as exc:
                last_error = exc
                log.warning("llm_invalid_output", extra={"task": task, "attempt": attempt})
                continue
            self._record(task, model, response)
            parsed = response.output_parsed
            if isinstance(parsed, schema):
                return parsed
            last_error = LLMError("no parsed output (refusal or empty answer)")
            log.warning("llm_no_output", extra={"task": task, "attempt": attempt})
        raise LLMError(f"task {task!r} failed after {MAX_ATTEMPTS} attempts: {last_error}")

    def embed(self, texts: list[str]) -> list[list[float]]:
        if not self.model_embed:
            raise LLMError("OPENAI_MODEL_EMBED not configured")
        response = self.client.embeddings.create(model=self.model_embed, input=texts)
        usage = getattr(response, "usage", None)
        self.usage.append(
            LLMUsage("embed", self.model_embed, getattr(usage, "prompt_tokens", 0) or 0, 0)
        )
        return [list(d.embedding) for d in response.data]

    def _record(self, task: str, model: str, response: Any) -> None:
        usage = getattr(response, "usage", None)
        entry = LLMUsage(
            task,
            model,
            getattr(usage, "input_tokens", 0) or 0,
            getattr(usage, "output_tokens", 0) or 0,
        )
        self.usage.append(entry)
        log.info(
            "llm_call",
            extra={"task": task, "model": model, "in_tokens": entry.input_tokens,
                   "out_tokens": entry.output_tokens},
        )  # fmt: skip


def _data_url(path: Path) -> str:
    mime = mimetypes.guess_type(path.name)[0] or "image/jpeg"
    return f"data:{mime};base64," + base64.b64encode(path.read_bytes()).decode("ascii")
