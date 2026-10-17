from pathlib import Path

import pytest

from reels.bot import texts
from reels.bot.files import looks_like_video, to_host_path
from reels.bot.keyboards import parse_callback, preview_keyboard
from reels.domain.segments import Segment


def test_host_path_mapping() -> None:
    host = to_host_path(
        "/var/lib/telegram-bot-api/123:ABC/documents/file_1.mp4",
        "/var/lib/telegram-bot-api",
        Path("data/telegram-bot-api"),
    )
    assert host == Path("data/telegram-bot-api/123:ABC/documents/file_1.mp4")


@pytest.mark.parametrize(
    "path", ["documents/file_1.mp4", "/tmp/file.mp4", "/var/lib/telegram-bot-api/../etc/passwd"]
)
def test_host_path_rejects_foreign_paths(path: str) -> None:
    with pytest.raises(ValueError):
        to_host_path(path, "/var/lib/telegram-bot-api", Path("/data"))


@pytest.mark.parametrize(
    ("name", "mime", "ok"),
    [("a.MOV", None, True), ("a.pdf", "application/pdf", False), (None, "video/mp4", True),
     ("clip", "application/octet-stream", False)],
)  # fmt: skip
def test_looks_like_video(name: str | None, mime: str | None, ok: bool) -> None:
    assert looks_like_video(name, mime) is ok


def test_callback_roundtrip_and_rejects_garbage() -> None:
    keyboard = preview_keyboard(42)
    approve, change = keyboard.inline_keyboard[0]
    assert parse_callback(approve.callback_data) == ("approve", 42)
    assert parse_callback(change.callback_data) == ("change", 42)
    assert parse_callback("apply:7") == ("apply", 7)
    assert parse_callback("retry:7") == ("retry", 7)
    for bad in (None, "", "approve", "approve:", "approve:x", "delete:1", "approve:-1"):
        assert parse_callback(bad) is None


@pytest.mark.parametrize(
    ("s", "text"), [(0.4, "0 s"), (45.4, "45 s"), (125, "2:05 min"), (180, "3:00 min")]
)
def test_duration(s: float, text: str) -> None:
    assert texts.duration(s) == text


def test_failure_texts() -> None:
    too_long = texts.failure_text("too_long", {"duration_s": 200.2, "limit_s": 180})
    assert "3:20 min" in too_long and "3:00 min" in too_long
    assert texts.failure_text("no_speech", {}) == texts.FAILED_NO_SPEECH
    assert texts.failure_text(None, {}) == texts.FAILED_GENERIC
    assert texts.failure_text("too_long", {}) == texts.FAILED_GENERIC  # details missing


def test_preview_messages_split_below_limit() -> None:
    segs = [Segment(number=i, first_word=i, last_word=i, text="x" * 50) for i in range(1, 41)]
    messages = texts.preview_messages(segs, limit=500)
    assert len(messages) > 1
    assert all(len(m) <= 500 for m in messages)
    joined = "\n".join(messages)
    assert "1. " in joined and "40. " in joined and texts.PREVIEW_QUESTION in messages[-1]


def test_preview_message_single() -> None:
    (msg,) = texts.preview_messages([Segment(number=1, first_word=0, last_word=1, text="Hallo.")])
    assert msg.startswith("Sätze in dieser Version:") and "1. Hallo." in msg


def test_admin_texts() -> None:
    text = texts.admin_job_failed(5, "render", 3, "FFmpegError: boom\ndetails", "Demo", 2, 1)
    assert (
        "Job 5 (render)" in text and "Ursache: FFmpegError: boom" in text and "details" not in text
    )
    status = texts.admin_status({"queued": 2}, {"review": 1}, 0, 75)
    assert "queued 2" in status and "1:15 min" in status
