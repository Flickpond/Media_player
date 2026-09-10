"""A real owner for integration tests.

`create_job` requires one now, and these tests talk to a real database, so a
fake UUID would be refused by the foreign key. This creates an actual row and
removes it — along with anything it owns — afterwards.
"""

import uuid

import pytest_asyncio
from sqlalchemy import delete
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from app.config import get_settings
from app.models.job import Job
from app.models.user import User


@pytest_asyncio.fixture
async def owner():
    """One throwaway account, cleaned up with every job it owns."""
    engine = create_async_engine(get_settings().postgres_dsn, poolclass=NullPool)
    factory = async_sessionmaker(engine, expire_on_commit=False)

    user = User(
        id=uuid.uuid4(),
        email=f"it-{uuid.uuid4().hex[:12]}@example.test",
        password_hash="not-a-real-hash",
        role="user",
    )
    async with factory() as session:
        session.add(user)
        await session.commit()

    yield user

    async with factory() as session:
        # Jobs first: the foreign key would otherwise refuse to drop the user.
        await session.execute(delete(Job).where(Job.owner_id == user.id))
        await session.execute(delete(User).where(User.id == user.id))
        await session.commit()
    await engine.dispose()
