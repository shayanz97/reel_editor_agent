"""Client glossary commands (4.9). Registered on the client router (access checked there)."""

from __future__ import annotations

from aiogram import Bot, Router
from aiogram.filters import Command, CommandObject
from aiogram.types import BufferedInputFile, Message
from sqlalchemy import Engine
from sqlalchemy.orm import Session

from reels.bot import texts
from reels.bot.common import db
from reels.bot.glossary_input import parse_entry, parse_lines
from reels.bot.service import Access
from reels.db import glossary
from reels.db.models import GlossaryStatus
from reels.domain.glossary import GlossaryTerm


async def on_list(message: Message, access: Access, engine: Engine) -> None:
    terms = await db(engine, lambda s: glossary.load_terms(s, access.client_id))
    if not terms:
        await message.answer(texts.GLOSSARY_EMPTY)
        return
    for chunk in texts.chunk_lines([*texts.glossary_lines(terms), "", texts.GLOSSARY_HELP]):
        await message.answer(chunk)


async def on_new(message: Message, command: CommandObject, access: Access, engine: Engine) -> None:
    try:
        entry = parse_entry(command.args or "")
    except ValueError:
        await message.answer(texts.GLOSSARY_USAGE_NEW)
        return
    _, created = await db(
        engine,
        lambda s: glossary.add_term(s, access.client_id, entry.term, entry.variants),
    )
    text = texts.GLOSSARY_ADDED if created else texts.GLOSSARY_UPDATED
    await message.answer(text.format(term=entry.term))


async def on_change(
    message: Message, command: CommandObject, access: Access, engine: Engine
) -> None:
    try:
        entry = parse_entry(command.args or "")
    except ValueError:
        await message.answer(texts.GLOSSARY_USAGE_CHANGE)
        return
    found = await db(
        engine, lambda s: glossary.set_variants(s, access.client_id, entry.term, entry.variants)
    )
    template = texts.GLOSSARY_UPDATED if found is not None else texts.GLOSSARY_NOT_FOUND
    await message.answer(template.format(term=entry.term))


async def on_delete(
    message: Message, command: CommandObject, access: Access, engine: Engine
) -> None:
    term = (command.args or "").strip()
    if not term:
        await message.answer(texts.GLOSSARY_USAGE_TERM.format(command="/begriff_loeschen"))
        return
    deleted = await db(engine, lambda s: glossary.delete_term(s, access.client_id, term))
    template = texts.GLOSSARY_DELETED if deleted else texts.GLOSSARY_NOT_FOUND
    await message.answer(template.format(term=term))


async def on_confirm(
    message: Message, command: CommandObject, access: Access, engine: Engine
) -> None:
    term = (command.args or "").strip()
    if not term:
        await message.answer(texts.GLOSSARY_USAGE_TERM.format(command="/begriff_ok"))
        return
    found = await db(engine, lambda s: glossary.confirm_term(s, access.client_id, term))
    template = texts.GLOSSARY_CONFIRMED if found is not None else texts.GLOSSARY_NOT_FOUND
    await message.answer(template.format(term=term))


async def on_import(
    message: Message, command: CommandObject, access: Access, engine: Engine
) -> None:
    entries, bad = parse_lines(command.args or "")
    if not entries:
        await message.answer(texts.GLOSSARY_USAGE_LIST)
        return
    terms = [GlossaryTerm(term=e.term, variants=list(e.variants), source="bot") for e in entries]

    def store(session: Session) -> tuple[int, int]:
        added = updated = 0
        for t in terms:
            _, created = glossary.add_term(
                session,
                access.client_id,
                t.term,
                t.variants,
                status=GlossaryStatus.CONFIRMED,
                source="bot",
            )
            added += created
            updated += not created
        return added, updated

    added, updated = await db(engine, store)
    await message.answer(texts.GLOSSARY_IMPORTED.format(added=added, updated=updated))
    if bad:
        await message.answer(texts.GLOSSARY_UNREADABLE.format(lines="; ".join(bad))[:4000])


async def on_export(message: Message, bot: Bot, access: Access, engine: Engine) -> None:
    terms = await db(engine, lambda s: glossary.load_terms(s, access.client_id))
    if not terms:
        await message.answer(texts.GLOSSARY_EMPTY)
        return
    data = glossary.export_csv(terms).encode("utf-8-sig")  # BOM: Excel reads umlauts correctly
    await bot.send_document(
        message.chat.id,
        BufferedInputFile(data, filename=f"woerterbuch_{access.client_slug}.csv"),
        caption=texts.GLOSSARY_EXPORT_CAPTION,
    )


def register(router: Router) -> None:
    router.message.register(on_list, Command("woerterbuch"))
    router.message.register(on_new, Command("begriff_neu"))
    router.message.register(on_change, Command("begriff_aendern"))
    router.message.register(on_delete, Command("begriff_loeschen"))
    router.message.register(on_confirm, Command("begriff_ok"))
    router.message.register(on_export, Command("begriffe_export"))
    router.message.register(on_import, Command("begriffe"))
