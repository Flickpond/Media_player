"""The worker state machine against a real PostgreSQL.

The unit tests in tests/test_worker_tasks.py use a fake that imitates the
repository's conditional-update semantics. This file is what proves the fake
tells the truth -- including the CHECK constraints, which only exist in the
database.

Run with a live stack:  RUN_POSTGRES_TESTS=1 pytest tests/integration
"""

import os
from uuid import UUID, uuid4

import pytest
import pytest_asyncio
from sqlalchemy import delete
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from app.config import get_settings
from app.models.job import Job, JobStatus
from app.repositories.jobs import create_job, get_job
from app.worker.storage import ObjectStoreError
from app.worker.tasks import JobOutcome, process_job_async

pytestmark = pytest.mark.skipif(
    os.getenv("RUN_POSTGRES_TESTS") != "1",
    reason="set RUN_POSTGRES_TESTS=1 to run PostgreSQL integration tests",
)


class FakeStep:
    def __init__(self, *, output_key: str | None = None, raises: Exception | None = None) -> None:
        self.output_key = output_key
        self.raises = raises

    def run(self, *, job_id: UUID, source_key: str) -> str:
        if self.raises is not None:
            raise self.raises
        return self.output_key or f"outputs/{job_id}/demo.mp4"


@pytest_asyncio.fixture
async def session_factory():
    engine = create_async_engine(get_settings().postgres_dsn, poolclass=NullPool)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    yield factory
    await engine.dispose()


async def seed_job(session_factory, job_id: UUID) -> None:
    async with session_factory() as session:
        await create_job(
            session,
            job_id=job_id,
            filename="demo.mp4",
            source_key=f"uploads/{job_id}/demo.mp4",
        )


async def cleanup(session_factory, job_id: UUID) -> None:
    async with session_factory() as session:
        await session.execute(delete(Job).where(Job.id == job_id))
        await session.commit()


async def test_success_path_persists_done_and_output_key(session_factory):
    job_id = uuid4()
    await seed_job(session_factory, job_id)
    try:
        outcome = await process_job_async(
            job_id,
            session_factory=session_factory,
            step=FakeStep(output_key=f"outputs/{job_id}/demo.mp4"),
        )

        assert outcome is JobOutcome.DONE
        async with session_factory() as session:
            job = await get_job(session, job_id)
        assert job.status == JobStatus.DONE.value
        assert job.output_key == f"outputs/{job_id}/demo.mp4"
        assert job.error is None
        assert job.updated_at >= job.created_at
    finally:
        await cleanup(session_factory, job_id)


async def test_failure_path_persists_failed_and_readable_error(session_factory):
    job_id = uuid4()
    await seed_job(session_factory, job_id)
    try:
        outcome = await process_job_async(
            job_id,
            session_factory=session_factory,
            step=FakeStep(raises=ObjectStoreError("source object missing from storage")),
        )

        assert outcome is JobOutcome.FAILED
        async with session_factory() as session:
            job = await get_job(session, job_id)
        assert job.status == JobStatus.FAILED.value
        assert job.error == "source object missing from storage"
        assert job.output_key is None
    finally:
        await cleanup(session_factory, job_id)


async def test_second_delivery_of_a_finished_job_changes_nothing(session_factory):
    job_id = uuid4()
    await seed_job(session_factory, job_id)
    try:
        await process_job_async(
            job_id, session_factory=session_factory, step=FakeStep(output_key="outputs/first.mp4")
        )
        async with session_factory() as session:
            after_first = await get_job(session, job_id)
        first_updated_at = after_first.updated_at

        outcome = await process_job_async(
            job_id, session_factory=session_factory, step=FakeStep(output_key="outputs/second.mp4")
        )

        assert outcome is JobOutcome.SKIPPED
        async with session_factory() as session:
            after_second = await get_job(session, job_id)
        assert after_second.output_key == "outputs/first.mp4"
        assert after_second.updated_at == first_updated_at
    finally:
        await cleanup(session_factory, job_id)


async def test_unknown_job_id_does_not_raise(session_factory):
    outcome = await process_job_async(uuid4(), session_factory=session_factory, step=FakeStep())

    assert outcome is JobOutcome.SKIPPED
