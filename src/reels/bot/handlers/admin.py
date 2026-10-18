"""Admin commands, only accepted in ADMIN_CHAT_ID."""

from __future__ import annotations

from aiogram import F, Router
from aiogram.filters import Command, CommandObject
from aiogram.types import Message
from sqlalchemy import Engine

from reels.bot import service, texts
from reels.bot.common import db
from reels.db.clients import ClientError


def parse_new_client(args: str | None) -> tuple[str, list[int]]:
    """'Müller GmbH | 123 456' -> ('Müller GmbH', [123, 456]); the '| ids' part is optional."""
    name, _, ids = (args or "").partition("|")
    if not name.strip():
        raise ValueError(texts.ADMIN_USAGE_NEW)
    try:
        user_ids = [int(x) for x in ids.split()]
    except ValueError as exc:
        raise ValueError(texts.ADMIN_USAGE_NEW) from exc
    return name.strip(), user_ids


async def on_new_client(message: Message, command: CommandObject, engine: Engine) -> None:
    try:
        name, user_ids = parse_new_client(command.args)
        client = await db(engine, lambda s: service.admin_create_client(s, name, user_ids))
    except (ValueError, ClientError) as exc:
        await message.answer(texts.ADMIN_ERROR.format(error=exc))
        return
    users = ", ".join(map(str, user_ids)) or "–"
    await message.answer(
        texts.ADMIN_CLIENT_CREATED.format(
            name=client.name, slug=client.slug, id=client.id, users=users
        )
    )


async def on_add_user(message: Message, command: CommandObject, engine: Engine) -> None:
    parts = (command.args or "").split()
    if len(parts) != 2 or not parts[1].isdigit():
        await message.answer(texts.ADMIN_USAGE_USER)
        return
    ref, user_id = parts[0], int(parts[1])
    try:
        client = await db(engine, lambda s: service.admin_add_user(s, ref, user_id))
    except ClientError as exc:
        await message.answer(texts.ADMIN_ERROR.format(error=exc))
        return
    await message.answer(
        texts.ADMIN_USER_ADDED.format(user_id=user_id, name=client.name, slug=client.slug)
    )


async def on_status(message: Message, engine: Engine) -> None:
    summary = await db(engine, service.status_summary)
    await message.answer(
        texts.admin_status(
            summary.jobs, summary.projects, summary.failed_24h, summary.oldest_queued_s
        )
    )


async def on_help(message: Message) -> None:
    """In the admin chat: the client help and the admin commands."""
    await message.answer(texts.HELP)
    await message.answer(texts.ADMIN_HELP)


def build_router(admin_chat_id: int) -> Router:
    router = Router(name="admin")
    router.message.filter(F.chat.id == admin_chat_id)
    router.message.register(on_new_client, Command("kunde_neu"))
    router.message.register(on_add_user, Command("kunde_user"))
    router.message.register(on_status, Command("status"))
    router.message.register(on_help, Command("admin", "hilfe"))
    return router
