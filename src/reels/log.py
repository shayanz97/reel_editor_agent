"""Structured logging: exactly one line per event.

Usage::

    log = get_logger(__name__)
    with log_context(client_id=3, project_id=17):
        log.info("transcribe_done", extra={"words": 412, "duration_s": 8.2})

`client_id`, `project_id`, `version_id` and `job_id` from the surrounding `log_context`
are attached to every record. Additional fields go into `extra`.
"""

from __future__ import annotations

import json
import logging
import sys
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from contextvars import ContextVar
from datetime import UTC, datetime
from types import MappingProxyType
from typing import Any, Literal, TextIO

_context: ContextVar[Mapping[str, Any]] = ContextVar(
    "reels_log_context", default=MappingProxyType({})
)

# Attributes every LogRecord has; everything else on a record came in via `extra`.
_RECORD_ATTRS = frozenset(vars(logging.LogRecord("", 0, "", 0, "", None, None))) | {
    "message",
    "asctime",
    "taskName",
}


@contextmanager
def log_context(**fields: Any) -> Iterator[None]:
    """Attach fields (e.g. client_id, job_id) to all log records inside the block."""
    merged = {**_context.get(), **{k: v for k, v in fields.items() if v is not None}}
    token = _context.set(merged)
    try:
        yield
    finally:
        _context.reset(token)


def _event_fields(record: logging.LogRecord) -> dict[str, Any]:
    fields: dict[str, Any] = dict(_context.get())
    for key, value in vars(record).items():
        if key not in _RECORD_ATTRS and not key.startswith("_"):
            fields[key] = value
    if record.exc_info:
        fields["exc"] = logging.Formatter().formatException(record.exc_info)
    return fields


def _timestamp(record: logging.LogRecord) -> str:
    return datetime.fromtimestamp(record.created, tz=UTC).isoformat(timespec="milliseconds")


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload = {
            "ts": _timestamp(record),
            "level": record.levelname,
            "logger": record.name,
            "event": record.getMessage(),
            **_event_fields(record),
        }
        # json.dumps escapes newlines, so the event stays on one line.
        return json.dumps(payload, ensure_ascii=False, default=str)


class TextFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        parts = [
            _timestamp(record),
            f"{record.levelname:<7}",
            record.name,
            _single_line(record.getMessage()),
        ]
        parts += [f"{k}={_text_value(v)}" for k, v in _event_fields(record).items()]
        return " ".join(parts)


def _single_line(text: str) -> str:
    return text.replace("\r", "\\r").replace("\n", "\\n")


def _text_value(value: Any) -> str:
    text = str(value)
    if not text or any(c.isspace() for c in text) or '"' in text or "=" in text:
        return json.dumps(text, ensure_ascii=False)
    return text


def configure_logging(
    level: str = "INFO",
    fmt: Literal["json", "text"] = "text",
    stream: TextIO | None = None,
) -> None:
    """Replace root handlers with a single structured handler."""
    handler = logging.StreamHandler(stream or sys.stderr)
    handler.setFormatter(JsonFormatter() if fmt == "json" else TextFormatter())
    root = logging.getLogger()
    root.handlers[:] = [handler]
    root.setLevel(level.upper())


def get_logger(name: str) -> logging.Logger:
    return logging.getLogger(name)
