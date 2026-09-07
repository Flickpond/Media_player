"""The API's engine wiring.

The point of these is the *timing*: the engine must be built when it is first
asked for, not when the module is imported. `Base` lives in `app.database`, so
every model, every test module and Alembic's `env.py` import it -- building an
engine on import would make a resolvable DSN a precondition for importing
anything under `app/`.
"""

from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker

from app import database
from app.config import Settings


def clear_caches() -> None:
    database.get_engine.cache_clear()
    database.get_session_factory.cache_clear()


def test_the_engine_reads_settings_when_it_is_called_not_when_imported(monkeypatch):
    """Settings patched *after* import still take effect, which they could not
    if the engine had been constructed at import time.
    """
    clear_caches()
    pinned = Settings(postgres_dsn="postgresql+asyncpg://u:p@built-on-demand.test:5432/db")
    monkeypatch.setattr(database, "get_settings", lambda: pinned)

    try:
        engine = database.get_engine()

        assert isinstance(engine, AsyncEngine)
        assert "built-on-demand.test" in str(engine.url)
    finally:
        clear_caches()


def test_the_engine_is_built_once_and_reused(monkeypatch):
    clear_caches()
    pinned = Settings(postgres_dsn="postgresql+asyncpg://u:p@once.test:5432/db")
    monkeypatch.setattr(database, "get_settings", lambda: pinned)

    try:
        assert database.get_engine() is database.get_engine()
    finally:
        clear_caches()


def test_the_session_factory_is_bound_to_that_engine(monkeypatch):
    clear_caches()
    pinned = Settings(postgres_dsn="postgresql+asyncpg://u:p@bound.test:5432/db")
    monkeypatch.setattr(database, "get_settings", lambda: pinned)

    try:
        factory = database.get_session_factory()

        assert isinstance(factory, async_sessionmaker)
        assert factory.kw["bind"] is database.get_engine()
    finally:
        clear_caches()
