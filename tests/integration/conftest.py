"""Fixtures for tests against the docker compose Postgres (database `reels_test`)."""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import Engine, make_url, text
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import Session

from reels.config import Settings
from reels.db.models import Base
from reels.db.session import make_engine

ROOT = Path(__file__).parents[2]


def alembic_config(url: str) -> Config:
    cfg = Config(str(ROOT / "alembic.ini"))
    cfg.set_main_option("script_location", str(ROOT / "alembic"))
    cfg.set_main_option("sqlalchemy.url", url.replace("%", "%%"))
    cfg.attributes["configure_logger"] = False
    return cfg


@pytest.fixture(scope="session")
def db_url() -> str:
    url = Settings().test_database_url
    database = make_url(url).database or ""
    # Tests drop and recreate everything: never run them against a real database.
    if not database.endswith("_test"):
        pytest.fail(f"TEST_DATABASE_URL must point to a *_test database, got {database!r}")
    engine = make_engine(url)
    try:
        with engine.connect():
            pass
    except OperationalError:
        target = make_url(url).render_as_string(hide_password=True)
        pytest.skip(f"test database {target} not reachable; run `docker compose up -d`")
    finally:
        engine.dispose()
    return url


@pytest.fixture(scope="session")
def migrated_db(db_url: str) -> str:
    cfg = alembic_config(db_url)
    command.downgrade(cfg, "base")
    command.upgrade(cfg, "head")
    return db_url


@pytest.fixture
def engine(migrated_db: str) -> Iterator[Engine]:
    eng = make_engine(migrated_db)
    yield eng
    tables = ", ".join(t.name for t in Base.metadata.sorted_tables)
    with eng.begin() as conn:
        conn.execute(text(f"TRUNCATE {tables} RESTART IDENTITY CASCADE"))
    eng.dispose()


@pytest.fixture
def session(engine: Engine) -> Iterator[Session]:
    with Session(engine) as s:
        yield s
