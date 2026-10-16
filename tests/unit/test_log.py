import io
import json
import logging
from collections.abc import Iterator

import pytest

from reels.log import configure_logging, get_logger, log_context


@pytest.fixture
def stream() -> Iterator[io.StringIO]:
    root = logging.getLogger()
    saved = root.handlers[:], root.level
    buf = io.StringIO()
    yield buf
    root.handlers[:], level = saved
    root.setLevel(level)


def _lines(buf: io.StringIO) -> list[str]:
    return buf.getvalue().splitlines()


def test_json_one_line_with_context_and_extra(stream: io.StringIO) -> None:
    configure_logging("INFO", "json", stream)
    log = get_logger("reels.test")
    with log_context(client_id=3, project_id=17):
        with log_context(job_id=99):
            log.info("step_done", extra={"step": "render", "duration_s": 1.5})
        log.info("outer")

    first, second = (json.loads(line) for line in _lines(stream))
    assert first["event"] == "step_done"
    assert first["level"] == "INFO"
    assert first["logger"] == "reels.test"
    assert (first["client_id"], first["project_id"], first["job_id"]) == (3, 17, 99)
    assert (first["step"], first["duration_s"]) == ("render", 1.5)
    assert "job_id" not in second
    assert second["project_id"] == 17


def test_context_omits_none_values(stream: io.StringIO) -> None:
    configure_logging("INFO", "json", stream)
    with log_context(client_id=1, job_id=None):
        get_logger("t").info("x")
    record = json.loads(_lines(stream)[0])
    assert record["client_id"] == 1
    assert "job_id" not in record


@pytest.mark.parametrize("fmt", ["json", "text"])
def test_exceptions_and_newlines_stay_on_one_line(stream: io.StringIO, fmt: str) -> None:
    configure_logging("INFO", fmt, stream)  # type: ignore[arg-type]
    log = get_logger("t")
    try:
        raise RuntimeError("boom\nsecond line")
    except RuntimeError:
        log.exception("failed\nhere", extra={"stderr": "a\nb"})
    lines = _lines(stream)
    assert len(lines) == 1
    assert "boom" in lines[0]


def test_text_format_key_values(stream: io.StringIO) -> None:
    configure_logging("DEBUG", "text", stream)
    with log_context(job_id=5):
        get_logger("reels.worker").debug("picked", extra={"step": "render", "note": "two words"})
    line = _lines(stream)[0]
    assert " DEBUG " in line
    assert "reels.worker picked" in line
    assert "job_id=5" in line
    assert "step=render" in line
    assert 'note="two words"' in line


def test_level_filters(stream: io.StringIO) -> None:
    configure_logging("WARNING", "text", stream)
    get_logger("t").info("hidden")
    get_logger("t").warning("shown")
    assert len(_lines(stream)) == 1
