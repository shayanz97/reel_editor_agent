"""Client side: access check, receiving videos, preview buttons."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from contextlib import suppress
from pathlib import Path
from typing import Any

from aiogram import BaseMiddleware, Bot, F, Router
from aiogram.filters import CommandStart
from aiogram.types import CallbackQuery, FSInputFile, Message, TelegramObject
from sqlalchemy import Engine

from reels.bot import service, texts
from reels.bot.common import FILE_TIMEOUT_S, db, notify_admin
from reels.bot.files import looks_like_video, to_host_path
from reels.bot.keyboards import APPROVE, parse_callback
from reels.bot.service import Access, Decision
from reels.config import Settings
from reels.log import get_logger, log_context

log = get_logger(__name__)


class AccessMiddleware(BaseMiddleware):
    """Only Telegram users listed in client_users get through. Unknown users: short refusal,
    the admin chat hears about each unknown user once per bot run."""

    def __init__(self) -> None:
        self.reported: set[int] = set()

    async def __call__(
        self,
        handler: Callable[[TelegramObject, dict[str, Any]], Awaitable[Any]],
        event: TelegramObject,
        data: dict[str, Any],
    ) -> Any:
        user = data.get("event_from_user")
        engine: Engine = data["engine"]
        if user is None:
            return None
        access = await db(engine, lambda s: service.find_access(s, user.id))
        if access is None:
            if isinstance(event, Message):
                await event.answer(texts.NOT_ALLOWED)
            elif isinstance(event, CallbackQuery):
                await event.answer(texts.NOT_ALLOWED, show_alert=True)
            if user.id not in self.reported:
                self.reported.add(user.id)
                username = f"@{user.username}" if user.username else "ohne Benutzernamen"
                await notify_admin(
                    data["bot"],
                    data["app"],
                    texts.ADMIN_UNKNOWN_USER.format(
                        name=user.full_name, username=username, user_id=user.id
                    ),
                )
            log.info("access_denied", extra={"telegram_user_id": user.id})
            return None
        data["access"] = access
        with log_context(client_id=access.client_id):
            return await handler(event, data)


async def on_start(message: Message) -> None:
    await message.answer(texts.START)


async def on_document(
    message: Message, bot: Bot, access: Access, engine: Engine, app: Settings
) -> None:
    doc = message.document
    assert doc is not None
    if not looks_like_video(doc.file_name, doc.mime_type):
        await message.answer(texts.NOT_A_VIDEO)
        return
    await receive_video(message, bot, access, engine, app, doc.file_id, doc.file_name)


async def on_compressed_video(
    message: Message, bot: Bot, access: Access, engine: Engine, app: Settings
) -> None:
    video = message.video
    assert video is not None
    await message.answer(texts.COMPRESSED_HINT)
    await receive_video(message, bot, access, engine, app, video.file_id, video.file_name)


async def on_other(message: Message) -> None:
    await message.answer(texts.NOT_A_VIDEO)


async def receive_video(
    message: Message,
    bot: Bot,
    access: Access,
    engine: Engine,
    app: Settings,
    file_id: str,
    file_name: str | None,
) -> None:
    chat_id, message_id = message.chat.id, message.message_id
    if await db(engine, lambda s: service.is_known_message(s, chat_id, message_id)):
        log.info("duplicate_update", extra={"message_id": message_id})
        return
    status = await message.reply(texts.DOWNLOADING)
    try:
        file = await bot.get_file(file_id, request_timeout=FILE_TIMEOUT_S)
        if not file.file_path:
            raise ValueError("Bot API returned no file path")
        source = to_host_path(
            file.file_path, app.telegram_files_container_path, app.telegram_files_host_path
        )
        project_id, version_id = await db(
            engine,
            lambda s: service.register_video(
                s,
                access,
                source,
                app,
                chat_id=chat_id,
                message_id=message_id,
                status_message_id=status.message_id,
                caption=message.caption,
                title=file_name,
            ),
        )
    except service.DuplicateMessageError:
        with suppress(Exception):
            await status.delete()
        return
    except Exception as exc:
        log.exception("video_receive_failed", extra={"message_id": message_id})
        await status.edit_text(texts.DOWNLOAD_FAILED)
        await notify_admin(
            bot,
            app,
            texts.ADMIN_DOWNLOAD_FAILED.format(
                client=access.client_name, message_id=message_id, cause=str(exc)[:300]
            ),
        )
        return
    await status.edit_text(texts.RECEIVED)
    log.info("video_received", extra={"project_id": project_id, "version_id": version_id})
    # The project has its own copy now; free the Bot API server's copy.
    with suppress(OSError):
        Path(source).unlink()


_DECISION_TEXT = {
    Decision.ALREADY_APPROVED: texts.ALREADY_APPROVED,
    Decision.OUTDATED: texts.OUTDATED,
    Decision.NOT_READY: texts.NOT_READY,
    Decision.FORBIDDEN: texts.FORBIDDEN,
}


async def on_button(
    callback: CallbackQuery, bot: Bot, access: Access, engine: Engine, app: Settings
) -> None:
    parsed = parse_callback(callback.data)
    if parsed is None:
        await callback.answer()
        return
    action, version_id = parsed
    user_id = callback.from_user.id
    if action == APPROVE:
        result = await db(engine, lambda s: service.approve(s, version_id, user_id, app))
    else:
        result = await db(engine, lambda s: service.change_request(s, version_id, user_id))
    if result.decision is not Decision.OK:
        await callback.answer(_DECISION_TEXT[result.decision], show_alert=True)
        return

    await callback.answer()
    message = callback.message
    chat_id = message.chat.id if message is not None else user_id
    if isinstance(message, Message):
        with suppress(Exception):
            await message.edit_reply_markup(reply_markup=None)
    if action == APPROVE:
        assert result.export is not None
        await bot.send_document(
            chat_id,
            FSInputFile(
                result.export, filename=f"reel_{result.project_id}_v{result.version_number}.mp4"
            ),
            caption=texts.APPROVED,
            request_timeout=FILE_TIMEOUT_S,
        )
        log.info("version_approved", extra={"version_id": version_id})
    else:
        await bot.send_message(chat_id, texts.CHANGE_NOT_YET)
        await notify_admin(
            bot,
            app,
            texts.ADMIN_CHANGE_REQUEST.format(
                client=result.client_name,
                project_id=result.project_id,
                number=result.version_number,
            ),
        )


def build_router() -> Router:
    router = Router(name="client")
    access = AccessMiddleware()
    router.message.middleware(access)
    router.callback_query.middleware(access)
    router.message.register(on_start, CommandStart())
    router.message.register(on_document, F.document)
    router.message.register(on_compressed_video, F.video)
    router.message.register(on_other)
    router.callback_query.register(on_button)
    return router
