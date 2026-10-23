"""The library trash against a real PostgreSQL: who sees a trashed job, and when it goes."""

import os
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
import pytest_asyncio
from sqlalchemy import update
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from app.config import get_settings
from app.models.job import Job
from app.repositories.jobs import (
    create_job,
    get_job,
    list_expired_trash,
    list_jobs,
    list_trash,
    restore_job,
    trash_job,
)

pytestmark = pytest.mark.skipif(
    os.getenv("RUN_POSTGRES_TESTS") != "1",
    reason="set RUN_POSTGRES_TESTS=1 to run PostgreSQL integration tests",
)


@pytest_asyncio.fixture
async def session_factory():
    engine = create_async_engine(get_settings().postgres_dsn, poolclass=NullPool)
    yield async_sessionmaker(engine, expire_on_commit=False)
    await engine.dispose()


async def _job(session, owner):
    job_id = uuid4()
    return await create_job(
        session, owner_id=owner.id, job_id=job_id, filename="demo.mp4",
        source_key=f"uploads/{job_id}/demo.mp4",
    )


@pytest.mark.asyncio
async def test_a_trashed_job_leaves_its_owners_library_and_page_but_not_the_worker(
    session_factory, owner
):
    async with session_factory() as session:
        kept, trashed = await _job(session, owner), await _job(session, owner)

        assert (await trash_job(session, trashed.id, owner_id=owner.id)).deleted_at is not None

        library = await list_jobs(session, owner_id=owner.id)
        assert [job.id for job in library] == [kept.id]
        assert await get_job(session, trashed.id, owner_id=owner.id) is None
        assert await get_job(session, trashed.id, owner_id=owner.id, include_trashed=True)
        # The worker and the reaper act on any job: one trashed mid-encode finishes.
        assert await get_job(session, trashed.id) is not None
        assert [job.id for job in await list_trash(session, owner_id=owner.id)] == [trashed.id]


@pytest.mark.asyncio
async def test_only_the_owner_can_trash_or_restore_and_only_once(session_factory, owner):
    async with session_factory() as session:
        job = await _job(session, owner)

        assert await trash_job(session, job.id, owner_id=uuid4()) is None
        assert await restore_job(session, job.id, owner_id=owner.id) is None, "not in the trash"
        assert await trash_job(session, job.id, owner_id=owner.id) is not None
        assert await trash_job(session, job.id, owner_id=owner.id) is None, "already there"
        assert await restore_job(session, job.id, owner_id=uuid4()) is None

        restored = await restore_job(session, job.id, owner_id=owner.id)
        assert restored is not None and restored.deleted_at is None
        assert [j.id for j in await list_jobs(session, owner_id=owner.id)] == [job.id]


@pytest.mark.asyncio
async def test_only_what_was_trashed_before_the_cutoff_has_expired(session_factory, owner):
    async with session_factory() as session:
        old = await _job(session, owner)
        recent = await _job(session, owner)
        kept = await _job(session, owner)
        now = datetime.now(UTC)
        for job, when in ((old, now - timedelta(days=31)), (recent, now - timedelta(days=2))):
            await session.execute(update(Job).where(Job.id == job.id).values(deleted_at=when))
        await session.commit()

        expired = await list_expired_trash(session, before=now - timedelta(days=30))
        ids = {job.id for job in expired}

        assert old.id in ids
        assert recent.id not in ids and kept.id not in ids
