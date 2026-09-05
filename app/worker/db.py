"""Database access for the worker process.

Deliberately separate from `app.database`, which the API uses. Each RQ task
runs its own `asyncio.run(...)`, so a pooled connection opened on a previous
event loop would be reused on a new one -- asyncpg rejects that. `NullPool`
opens a fresh connection per task and closes it at the end, which is the right
trade here: jobs are long relative to connect cost, and it keeps workers
genuinely stateless (N5).
"""

from functools import lru_cache

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from app.config import get_settings


@lru_cache
def get_worker_session_factory() -> async_sessionmaker[AsyncSession]:
    engine = create_async_engine(get_settings().postgres_dsn, poolclass=NullPool)
    return async_sessionmaker(engine, expire_on_commit=False)
