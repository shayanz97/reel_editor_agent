from collections.abc import Iterator

import pytest
from sqlalchemy import Engine
from sqlalchemy.orm import Session
from typer.testing import CliRunner

from reels.cli import app
from reels.config import get_settings
from reels.db.clients import get_client
from reels.db.session import get_engine
from reels.settings_schema import ClientSettings

pytestmark = pytest.mark.integration

runner = CliRunner()


@pytest.fixture(autouse=True)
def cli_uses_test_db(
    engine: Engine, migrated_db: str, monkeypatch: pytest.MonkeyPatch
) -> Iterator[None]:
    monkeypatch.setenv("DATABASE_URL", migrated_db)
    get_settings.cache_clear()
    get_engine.cache_clear()
    yield
    get_engine().dispose()
    get_settings.cache_clear()
    get_engine.cache_clear()


def test_client_add_creates_client_with_defaults(engine: Engine) -> None:
    result = runner.invoke(app, ["client", "add", "Demo", "--telegram-user", "123"])
    assert result.exit_code == 0, result.output
    assert "slug=demo" in result.output

    with Session(engine) as session:
        client = get_client(session, "demo")
        assert client is not None
        assert client.name == "Demo"
        assert [u.telegram_user_id for u in client.users] == [123]
        assert client.client_settings() == ClientSettings()
        assert get_client(session, str(client.id)) is client


def test_client_add_multiple_users_and_custom_slug(engine: Engine) -> None:
    result = runner.invoke(
        app,
        ["client", "add", "Müller GmbH", "--slug", "mg", "--telegram-user", "1",
         "--telegram-user", "2"],
    )  # fmt: skip
    assert result.exit_code == 0, result.output
    with Session(engine) as session:
        client = get_client(session, "mg")
        assert client is not None
        assert sorted(u.telegram_user_id for u in client.users) == [1, 2]


def test_client_add_rejects_duplicates() -> None:
    assert runner.invoke(app, ["client", "add", "Demo", "--telegram-user", "123"]).exit_code == 0

    same_slug = runner.invoke(app, ["client", "add", "demo"])
    assert same_slug.exit_code == 1
    assert "already exists" in same_slug.output

    same_user = runner.invoke(app, ["client", "add", "Other", "--telegram-user", "123"])
    assert same_user.exit_code == 1
    assert "already assigned" in same_user.output


def test_client_list() -> None:
    assert "no clients" in runner.invoke(app, ["client", "list"]).output
    runner.invoke(app, ["client", "add", "Demo", "--telegram-user", "123"])
    result = runner.invoke(app, ["client", "list"])
    assert result.exit_code == 0
    assert "demo" in result.output
    assert "123" in result.output
