"""Fake Telegram objects and the `bot_env` fixture for bot tests (no network)."""

import asyncio
import shutil
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pytest
from aiogram.exceptions import TelegramAPIError, TelegramBadRequest
from aiogram.methods import SendMessage
from sqlalchemy import Engine
from sqlalchemy.orm import Session

from reels.bot import service
from reels.bot.notifier import Notifier
from reels.config import get_settings
from reels.db.clients import create_client
from tests.fixtures.synthetic_video import make_video

ME, STRANGER, ADMIN = 1001, 6666, 9999
CONTAINER = "/var/lib/telegram-bot-api"


# --- fakes ------------------------------------------------------------------------


@dataclass
class Sent:
    chat_id: int
    text: str
    reply_markup: Any = None
    message_id: int = 0


@dataclass
class FakeBot:
    file_path: str | None = None
    messages: list[Sent] = field(default_factory=list)
    edits: list[tuple[int, int, str]] = field(default_factory=list)
    videos: list[tuple[int, Path, dict[str, Any]]] = field(default_factory=list)
    documents: list[tuple[int, Path, str | None]] = field(default_factory=list)
    document_data: list[bytes] = field(default_factory=list)
    photos: list[tuple[int, Path, Sent]] = field(default_factory=list)
    fail_edit: bool = False
    fail_send: bool = False
    next_id: int = 500

    async def get_file(self, file_id: str, request_timeout: int | None = None) -> Any:
        return type("File", (), {"file_path": self.file_path})()

    async def send_message(self, chat_id: int, text: str, reply_markup: Any = None) -> Sent:
        if self.fail_send:
            raise TelegramAPIError(
                SendMessage(chat_id=chat_id, text=text), "Forbidden: bot was blocked"
            )
        self.next_id += 1
        sent = Sent(chat_id, text, reply_markup, self.next_id)
        self.messages.append(sent)
        return sent

    async def edit_message_text(self, text: str, chat_id: int, message_id: int) -> None:
        if self.fail_edit:
            raise TelegramBadRequest(
                SendMessage(chat_id=chat_id, text=text), "message to edit not found"
            )
        self.edits.append((chat_id, message_id, text))

    async def send_video(self, chat_id: int, video: Any, **kwargs: Any) -> None:
        self.videos.append((chat_id, Path(video.path), kwargs))

    async def send_photo(self, chat_id: int, photo: Any, caption: str | None = None,
                         reply_markup: Any = None) -> Sent:  # fmt: skip
        self.next_id += 1
        sent = Sent(chat_id, caption or "", reply_markup, self.next_id)
        self.photos.append((chat_id, Path(photo.path), sent))
        return sent

    async def send_document(self, chat_id: int, document: Any, caption: str | None = None,
                            request_timeout: int | None = None) -> None:  # fmt: skip
        path = getattr(document, "path", None)
        self.documents.append((chat_id, Path(path) if path else Path(document.filename), caption))
        self.document_data.append(getattr(document, "data", b""))


@dataclass
class FakeStatus:
    message_id: int
    texts: list[str] = field(default_factory=list)
    deleted: bool = False

    async def edit_text(self, text: str) -> None:
        self.texts.append(text)

    async def delete(self) -> None:
        self.deleted = True


@dataclass
class FakeMessage:
    chat_id: int
    message_id: int
    document: Any = None
    video: Any = None
    caption: str | None = None
    text: str | None = None
    reply_to_message: Any = None
    answers: list[str] = field(default_factory=list)
    status: FakeStatus | None = None

    @property
    def chat(self) -> Any:
        return type("Chat", (), {"id": self.chat_id})()

    async def answer(self, text: str) -> None:
        self.answers.append(text)

    async def reply(self, text: str) -> FakeStatus:
        self.status = FakeStatus(message_id=self.message_id + 1000, texts=[text])
        return self.status


@dataclass
class FakeCallback:
    data: str
    user_id: int
    alerts: list[str | None] = field(default_factory=list)
    message: Any = None

    @property
    def from_user(self) -> Any:
        return type("User", (), {"id": self.user_id})()

    async def answer(self, text: str | None = None, show_alert: bool = False) -> None:
        self.alerts.append(text)


def _doc(name: str = "clip.mp4", mime: str = "video/mp4") -> Any:
    return type("Doc", (), {"file_id": "F1", "file_name": name, "mime_type": mime})()


# --- fixtures -----------------------------------------------------------------------


@pytest.fixture
def bot_env(
    app_env: Path, engine: Engine, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> tuple[FakeBot, service.Access, Path]:
    host = tmp_path / "tgapi"
    monkeypatch.setenv("TELEGRAM_FILES_HOST_PATH", str(host))
    monkeypatch.setenv("TELEGRAM_FILES_CONTAINER_PATH", CONTAINER)
    monkeypatch.setenv("ADMIN_CHAT_ID", str(ADMIN))
    get_settings.cache_clear()

    spec = make_video(tmp_path / "src.mp4", "portrait", size=(360, 640))
    monkeypatch.setenv("REELS_TEST_SPEC", str(tmp_path / "src.json"))
    upload = host / "TOKEN" / "documents" / "file_1.mp4"
    upload.parent.mkdir(parents=True)
    shutil.copy(spec.path, upload)

    with Session(engine) as s:
        create_client(s, "Demo", telegram_user_ids=[ME])
        s.commit()
        access = service.find_access(s, ME)
    assert access is not None
    return FakeBot(file_path=f"{CONTAINER}/TOKEN/documents/file_1.mp4"), access, upload


def _go(coro: Any) -> Any:
    return asyncio.run(coro)


async def _tick(bot: FakeBot, engine: Engine) -> int:
    return await Notifier(bot, engine, get_settings()).tick()  # type: ignore[arg-type]
