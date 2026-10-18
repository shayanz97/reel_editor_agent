"""Telegram command menu: client commands for everyone, plus admin commands in the admin chat."""

from __future__ import annotations

from collections.abc import Sequence

from aiogram import Bot
from aiogram.types import (
    BotCommand,
    BotCommandScopeAllPrivateChats,
    BotCommandScopeChat,
    BotCommandScopeDefault,
)

from reels.bot import texts
from reels.config import Settings
from reels.log import get_logger

log = get_logger(__name__)


def commands(pairs: Sequence[tuple[str, str]]) -> list[BotCommand]:
    return [BotCommand(command=name, description=text) for name, text in pairs]


async def setup_menu(bot: Bot, app: Settings) -> None:
    """Set menu and descriptions on every start (idempotent). The most specific scope wins:
    the admin chat sees client + admin commands, every other chat the client commands."""
    client = commands(texts.CLIENT_COMMANDS)
    await bot.set_my_commands(client, scope=BotCommandScopeDefault())
    await bot.set_my_commands(client, scope=BotCommandScopeAllPrivateChats())
    if app.admin_chat_id is not None:
        both = commands(texts.CLIENT_COMMANDS + texts.ADMIN_COMMANDS)
        await bot.set_my_commands(both, scope=BotCommandScopeChat(chat_id=app.admin_chat_id))
    await bot.set_my_short_description(texts.BOT_SHORT_DESCRIPTION)
    await bot.set_my_description(texts.BOT_DESCRIPTION)
    log.info("bot_menu_set", extra={"admin_menu": app.admin_chat_id is not None})
