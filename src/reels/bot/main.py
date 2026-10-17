"""Telegram bot process (aiogram, polling, local Bot API server).

    uv run python -m reels.bot
    uv run python -m reels.bot --logout-cloud   # once, before the first local start

The bot never processes media: it registers videos as projects (the worker does the rest)
and reports back what the worker did.
"""

from __future__ import annotations

import argparse
import asyncio

from aiogram import Bot, Dispatcher
from aiogram.client.session.aiohttp import AiohttpSession
from aiogram.client.telegram import TelegramAPIServer
from sqlalchemy import Engine

from reels.bot.handlers import admin, client
from reels.bot.notifier import Notifier, stop
from reels.config import Settings, get_settings
from reels.db.session import make_engine
from reels.log import configure_logging, get_logger

log = get_logger(__name__)


def build_dispatcher(app: Settings) -> Dispatcher:
    dp = Dispatcher()
    if app.admin_chat_id is not None:
        dp.include_router(admin.build_router(app.admin_chat_id))
    else:
        log.warning("admin_chat_missing")
    dp.include_router(client.build_router())
    return dp


def local_bot(app: Settings) -> Bot:
    if app.telegram_bot_token is None:
        raise SystemExit("TELEGRAM_BOT_TOKEN is not set in .env")
    session = AiohttpSession(api=TelegramAPIServer.from_base(app.telegram_api_url, is_local=True))
    return Bot(app.telegram_bot_token.get_secret_value(), session=session)


async def run(app: Settings, engine: Engine) -> None:
    bot = local_bot(app)
    me = await bot.get_me()
    log.info("bot_started", extra={"username": me.username, "api": app.telegram_api_url})
    notifier = asyncio.create_task(Notifier(bot, engine, app).run())
    try:
        await build_dispatcher(app).start_polling(
            bot, allowed_updates=["message", "callback_query"], engine=engine, app=app
        )
    finally:
        await stop(notifier)
        await bot.session.close()


async def logout_cloud(app: Settings) -> None:
    """Log the bot out of api.telegram.org; required once before using a local server."""
    if app.telegram_bot_token is None:
        raise SystemExit("TELEGRAM_BOT_TOKEN is not set in .env")
    bot = Bot(app.telegram_bot_token.get_secret_value())
    try:
        await bot.log_out()
    finally:
        await bot.session.close()
    print("Logged out from the cloud Bot API. Start the bot now: uv run python -m reels.bot")


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(prog="python -m reels.bot", description=__doc__)
    parser.add_argument(
        "--logout-cloud",
        action="store_true",
        help="log the bot out of api.telegram.org (once, before the first local start)",
    )
    args = parser.parse_args(argv)
    app = get_settings()
    configure_logging(app.log_level, app.log_format)
    if args.logout_cloud:
        asyncio.run(logout_cloud(app))
        return
    engine = make_engine(app.database_url)
    try:
        asyncio.run(run(app, engine))
    finally:
        engine.dispose()
