from collections.abc import AsyncIterator
from functools import lru_cache

from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.orm import DeclarativeBase

from app.config import get_settings


class Base(DeclarativeBase):
    pass


@lru_cache
def get_engine() -> AsyncEngine:
    """Build the API's engine on first use, not at import.

    `Base` lives in this module, so every model, every test module and Alembic's
    `env.py` import it. Creating an engine as a side effect of that would mean
    nothing under `app/` can be imported without a resolvable DSN in the
    environment -- a coupling that spreads to each new importer and gets wider
    the longer it stands. `app/worker/db.py` already takes this shape.
    """
    return create_async_engine(get_settings().postgres_dsn, pool_pre_ping=True)


@lru_cache
def get_session_factory() -> async_sessionmaker[AsyncSession]:
    return async_sessionmaker(get_engine(), expire_on_commit=False)


async def get_session() -> AsyncIterator[AsyncSession]:
    async with get_session_factory()() as session:
        yield session
