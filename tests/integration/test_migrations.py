import pytest
from alembic import command
from alembic.autogenerate import compare_metadata
from alembic.migration import MigrationContext
from sqlalchemy import inspect, text

from reels.db.models import Base
from reels.db.session import make_engine
from tests.integration.conftest import alembic_config

pytestmark = pytest.mark.integration

TABLES = {
    "clients",
    "client_users",
    "projects",
    "assets",
    "transcripts",
    "versions",
    "corrections",
    "glossary",
    "jobs",
}


def test_upgrade_creates_all_tables_and_pgvector(migrated_db: str) -> None:
    engine = make_engine(migrated_db)
    with engine.connect() as conn:
        assert set(inspect(conn).get_table_names()) >= TABLES
        ext = conn.scalar(text("SELECT extname FROM pg_extension WHERE extname = 'vector'"))
        assert ext == "vector"
    engine.dispose()


def test_models_match_migrations(migrated_db: str) -> None:
    """Fails if a model was changed without a matching migration."""
    engine = make_engine(migrated_db)
    with engine.connect() as conn:
        ctx = MigrationContext.configure(conn, opts={"compare_type": True})
        diff = compare_metadata(ctx, Base.metadata)
    engine.dispose()
    assert diff == []


def test_downgrade_and_upgrade_again(migrated_db: str) -> None:
    cfg = alembic_config(migrated_db)
    command.downgrade(cfg, "base")
    engine = make_engine(migrated_db)
    with engine.connect() as conn:
        assert not TABLES & set(inspect(conn).get_table_names())
    command.upgrade(cfg, "head")
    with engine.connect() as conn:
        assert set(inspect(conn).get_table_names()) >= TABLES
    engine.dispose()
