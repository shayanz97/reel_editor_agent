import asyncio
import re
from typing import Any

from aiogram.filters import Command, CommandStart

from reels.bot import texts
from reels.bot.handlers import admin, client
from reels.bot.menu import setup_menu
from reels.config import Settings

NAME = re.compile(r"^[a-z0-9_]{1,32}$")


def _registered(router: Any) -> set[str]:
    names: set[str] = set()
    for handler in router.message.handlers:
        for f in handler.filters:
            cb = f.callback
            if isinstance(cb, CommandStart):
                names.add("start")
            elif isinstance(cb, Command):
                names |= {c for c in cb.commands if isinstance(c, str)}
    return names


def test_menu_entries_valid_for_telegram() -> None:
    all_commands = texts.CLIENT_COMMANDS + texts.ADMIN_COMMANDS
    names = [n for n, _ in all_commands]
    assert len(names) == len(set(names))
    for name, description in all_commands:
        assert NAME.match(name), name
        assert 3 <= len(description) <= 256, name
    assert len(texts.BOT_SHORT_DESCRIPTION) <= 120
    assert len(texts.BOT_DESCRIPTION) <= 512


def test_every_menu_entry_has_a_handler() -> None:
    client_names = _registered(client.build_router())
    admin_names = _registered(admin.build_router(admin_chat_id=1))
    assert {n for n, _ in texts.CLIENT_COMMANDS} <= client_names
    assert {n for n, _ in texts.ADMIN_COMMANDS} <= admin_names


class _Bot:
    def __init__(self) -> None:
        self.commands: list[tuple[list[str], Any]] = []
        self.descriptions: list[str] = []

    async def set_my_commands(self, commands: list[Any], scope: Any = None) -> None:
        self.commands.append(([c.command for c in commands], scope))

    async def set_my_description(self, text: str) -> None:
        self.descriptions.append(text)

    async def set_my_short_description(self, text: str) -> None:
        self.descriptions.append(text)


def test_admin_chat_gets_admin_commands_too() -> None:
    bot = _Bot()
    asyncio.run(setup_menu(bot, Settings(_env_file=None, admin_chat_id=42)))  # type: ignore[arg-type, call-arg]
    by_scope = {c[1].type: c for c in bot.commands}
    assert "kunde_neu" not in by_scope["all_private_chats"][0]
    assert by_scope["default"][0] == by_scope["all_private_chats"][0]
    admin_menu, scope = by_scope["chat"]
    assert scope.chat_id == 42 and "kunde_neu" in admin_menu and "woerterbuch" in admin_menu
    assert texts.BOT_DESCRIPTION in bot.descriptions


def test_no_admin_menu_without_admin_chat() -> None:
    bot = _Bot()
    asyncio.run(setup_menu(bot, Settings(_env_file=None)))  # type: ignore[arg-type, call-arg]
    assert all(scope.type != "chat" for _, scope in bot.commands)


def test_help_texts() -> None:
    from tests.integration.bot_fakes import FakeMessage

    m = FakeMessage(1, 1)
    asyncio.run(client.on_help(m))  # type: ignore[arg-type]
    asyncio.run(admin.on_help(m))  # type: ignore[arg-type]
    assert m.answers == [texts.HELP, texts.HELP, texts.ADMIN_HELP]
    assert "/hilfe" in texts.START
