"""Helpers shared by handlers and the notifier."""

from __future__ import annotations

import asyncio
from collections.abc import Callable

from aiogram import Bot
from aiogram.exceptions import TelegramAPIError
from sqlalchemy import Engine
from sqlalchemy.orm import Session

from reels.config import Settings
from reels.log import get_logger

log = get_logger(__name__)

# Downloads and uploads of large videos through the local Bot API server.
FILE_TIMEOUT_S = 600


async def db[T](engine: Engine, fn: Callable[[Session], T]) -> T:
    """Run a synchronous DB function in a thread, in its own committed transaction."""

    def call() -> T:
        with Session(engine, expire_on_commit=False) as session:
            result = fn(session)
            session.commit()
            return result

    return await asyncio.to_thread(call)


async def notify_admin(bot: Bot, app: Settings, text: str) -> None:
    if app.admin_chat_id is None:
        log.warning("admin_notify_no_chat", extra={"text": text})
        return
    try:
        await bot.send_message(app.admin_chat_id, text)
    except TelegramAPIError:
        log.exception("admin_notify_failed", extra={"text": text})
