"""Bot end to end without Telegram: fake bot/messages, real DB, real worker steps."""

import shutil
from pathlib import Path
from typing import Any

import pytest
from aiogram.filters import CommandObject
from sqlalchemy import Engine, select
from sqlalchemy.orm import Session

from reels.bot import service, texts
from reels.bot.handlers import admin, client
from reels.bot.notifier import MAX_SEND_FAILURES, Notifier
from reels.config import get_settings
from reels.db.models import Client, Job, Project, ProjectStatus, Version
from tests.integration.bot_fakes import (
    ADMIN,
    ME,
    STRANGER,
    FakeBot,
    FakeCallback,
    FakeMessage,
    _doc,
    _go,
    _tick,
)
from tests.integration.test_pipeline_synthetic import _run

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="ffmpeg not installed"),
]

# --- tests ----------------------------------------------------------------------------


def test_full_flow_receive_preview_approve(
    bot_env: tuple[FakeBot, service.Access, Path], engine: Engine
) -> None:
    bot, access, upload = bot_env
    app = get_settings()
    msg = FakeMessage(ME, 10, document=_doc(), caption="Bitte zügig")

    _go(client.on_document(msg, bot, access, engine, app))  # type: ignore[arg-type]
    assert msg.status is not None and msg.status.texts == [texts.DOWNLOADING, texts.RECEIVED]
    assert not upload.exists(), "Bot API copy is removed after registering"
    with Session(engine) as s:
        project = s.scalars(select(Project)).one()
        assert (project.caption, project.telegram_message_id) == ("Bitte zügig", 10)
        assert project.status_message_id == msg.status.message_id

    # Same update delivered again: ignored.
    again = FakeMessage(ME, 10, document=_doc())
    _go(client.on_document(again, bot, access, engine, app))  # type: ignore[arg-type]
    assert again.status is None and again.answers == []

    assert _go(_tick(bot, engine)) == 0  # nothing to tell yet
    _run(max_jobs=1)  # probe -> project processing
    assert _go(_tick(bot, engine)) == 1
    assert bot.edits[-1] == (ME, msg.status.message_id, texts.PROCESSING)

    _run()  # remaining steps
    assert _go(_tick(bot, engine)) == 1
    assert bot.edits[-1][2] == texts.PREVIEW_READY_STATUS.format(number=1)
    ((chat, video_path, kwargs),) = bot.videos
    assert chat == ME and video_path.name == "export.mp4" and kwargs["width"] == 1080
    preview = bot.messages[-1]
    assert "1. " in preview.text and texts.PREVIEW_QUESTION in preview.text
    approve_button = preview.reply_markup.inline_keyboard[0][0]
    assert _go(_tick(bot, engine)) == 0, "preview is sent once"

    callback = FakeCallback(approve_button.callback_data, ME)
    _go(client.on_button(callback, bot, access, engine, app))  # type: ignore[arg-type]
    ((doc_chat, doc_path, caption),) = bot.documents
    assert (doc_chat, doc_path, caption) == (ME, video_path, texts.APPROVED)
    with Session(engine) as s:
        project = s.scalars(select(Project)).one()
        assert project.status is ProjectStatus.APPROVED and project.approved_at is not None
        assert s.scalars(select(Version)).one().preview_message_id == preview.message_id

    second = FakeCallback(approve_button.callback_data, ME)
    _go(client.on_button(second, bot, access, engine, app))  # type: ignore[arg-type]
    assert second.alerts == [texts.ALREADY_APPROVED] and len(bot.documents) == 1


def test_worker_failure_tells_client_and_admin(
    bot_env: tuple[FakeBot, service.Access, Path], engine: Engine
) -> None:
    bot, access, _ = bot_env
    with Session(engine) as s:
        demo = s.scalars(select(Client)).one()
        demo.settings = {**demo.settings, "max_duration_s": 2}
        s.commit()
    msg = FakeMessage(ME, 11, document=_doc())
    _go(client.on_document(msg, bot, access, engine, get_settings()))  # type: ignore[arg-type]
    _run()
    assert _go(_tick(bot, engine)) == 2
    client_text = bot.edits[-1][2]
    assert client_text == texts.failure_text("too_long", {"duration_s": 6.6, "limit_s": 2})
    admin_msg = next(m for m in bot.messages if m.chat_id == ADMIN)
    with Session(engine) as s:
        job = s.scalars(select(Job)).one()
    assert f"Job {job.id} (probe)" in admin_msg.text and "Demo" in admin_msg.text
    assert _go(_tick(bot, engine)) == 0


def test_change_button_asks_for_text(
    bot_env: tuple[FakeBot, service.Access, Path], engine: Engine
) -> None:
    bot, access, _ = bot_env
    app = get_settings()
    _go(client.on_document(FakeMessage(ME, 12, document=_doc()), bot, access, engine, app))  # type: ignore[arg-type]
    _run()
    with Session(engine) as s:
        version_id = s.scalars(select(Version)).one().id
    callback = FakeCallback(f"change:{version_id}", ME)
    _go(client.on_button(callback, bot, access, engine, app))  # type: ignore[arg-type]
    assert [m.text for m in bot.messages if m.chat_id == ME] == [texts.CHANGE_PROMPT]

    forged = FakeCallback("approve:999", ME)
    _go(client.on_button(forged, bot, access, engine, app))  # type: ignore[arg-type]
    assert forged.alerts == [texts.FORBIDDEN]


def test_receive_edge_cases(bot_env: tuple[FakeBot, service.Access, Path], engine: Engine) -> None:
    bot, access, _ = bot_env
    app = get_settings()

    pdf = FakeMessage(ME, 20, document=_doc("a.pdf", "application/pdf"))
    _go(client.on_document(pdf, bot, access, engine, app))  # type: ignore[arg-type]
    assert pdf.answers == [texts.NOT_A_VIDEO]

    compressed = FakeMessage(ME, 21, video=type("V", (), {"file_id": "F2", "file_name": None})())
    _go(client.on_compressed_video(compressed, bot, access, engine, app))  # type: ignore[arg-type]
    assert compressed.answers == [texts.COMPRESSED_HINT]
    assert compressed.status is not None and compressed.status.texts[-1] == texts.RECEIVED

    bot.file_path = "/somewhere/else/file.mp4"  # server not in --local mode / wrong volume
    broken = FakeMessage(ME, 22, document=_doc())
    _go(client.on_document(broken, bot, access, engine, app))  # type: ignore[arg-type]
    assert broken.status is not None and broken.status.texts[-1] == texts.DOWNLOAD_FAILED
    assert any(m.chat_id == ADMIN and "Download fehlgeschlagen" in m.text for m in bot.messages)
    with Session(engine) as s:
        assert len(s.scalars(select(Project)).all()) == 1  # only the compressed one


def test_access_middleware(bot_env: tuple[FakeBot, service.Access, Path], engine: Engine) -> None:
    bot, _, _ = bot_env
    middleware = client.AccessMiddleware()
    calls: list[Any] = []

    async def handler(event: Any, data: dict[str, Any]) -> str:
        calls.append(data["access"])
        return "handled"

    def data(user_id: int) -> dict[str, Any]:
        user = type("U", (), {"id": user_id, "username": None, "full_name": "Fremd"})()
        return {"event_from_user": user, "engine": engine, "app": get_settings(), "bot": bot}

    assert _go(middleware(handler, object(), data(ME))) == "handled"  # type: ignore[arg-type]
    assert calls[0].client_slug == "demo"
    assert _go(middleware(handler, object(), data(STRANGER))) is None  # type: ignore[arg-type]
    assert _go(middleware(handler, object(), data(STRANGER))) is None  # type: ignore[arg-type]
    admin_notes = [m for m in bot.messages if m.chat_id == ADMIN]
    assert len(admin_notes) == 1 and str(STRANGER) in admin_notes[0].text
    assert len(calls) == 1


def test_notifier_falls_back_and_gives_up(
    bot_env: tuple[FakeBot, service.Access, Path], engine: Engine
) -> None:
    bot, access, _ = bot_env
    _go(
        client.on_document(
            FakeMessage(ME, 30, document=_doc()), bot, access, engine, get_settings()
        )
    )  # type: ignore[arg-type]
    _run(max_jobs=1)

    bot.fail_edit = True  # status message deleted by the client -> send a new one
    assert _go(_tick(bot, engine)) == 1
    assert bot.messages[-1].text == texts.PROCESSING

    _run()
    bot.fail_send = True  # client blocked the bot
    notifier = Notifier(bot, engine, get_settings())  # type: ignore[arg-type]
    results = [_go(notifier.tick()) for _ in range(MAX_SEND_FAILURES)]
    assert results == [0] * (MAX_SEND_FAILURES - 1) + [1]
    assert _go(notifier.tick()) == 0


def test_admin_commands(app_env: Path, engine: Engine) -> None:
    assert admin.parse_new_client("Müller GmbH | 123 456") == ("Müller GmbH", [123, 456])
    assert admin.parse_new_client("Studio 54") == ("Studio 54", [])
    for bad in (None, "", " | 1", "X | abc"):
        with pytest.raises(ValueError):
            admin.parse_new_client(bad)

    msg = FakeMessage(ADMIN, 1)
    _go(
        admin.on_new_client(msg, CommandObject(command="kunde_neu", args="Neu GmbH | 4242"), engine)
    )  # type: ignore[arg-type]
    _go(admin.on_add_user(msg, CommandObject(command="kunde_user", args="neu-gmbh 4343"), engine))  # type: ignore[arg-type]
    _go(admin.on_add_user(msg, CommandObject(command="kunde_user", args="neu-gmbh 4343"), engine))  # type: ignore[arg-type]
    _go(admin.on_add_user(msg, CommandObject(command="kunde_user", args="nur-eins"), engine))  # type: ignore[arg-type]
    _go(admin.on_status(msg, engine))  # type: ignore[arg-type]
    created, added, duplicate, usage, status = msg.answers
    assert "Neu GmbH (Kürzel neu-gmbh" in created and "4242" in created
    assert "4343" in added
    assert duplicate.startswith("Fehler:") and usage == texts.ADMIN_USAGE_USER
    assert status.startswith("Jobs:")
