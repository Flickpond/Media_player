import os
from uuid import uuid4

import pytest
import pytest_asyncio
from sqlalchemy import delete
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from app.config import get_settings
from app.models.job import Job, JobStatus
from app.repositories.jobs import (
    InvalidJobTransitionError,
    create_job,
    get_job,
    mark_done,
    mark_failed,
    mark_processing,
)

pytestmark = pytest.mark.skipif(
    os.getenv("RUN_POSTGRES_TESTS") != "1",
    reason="set RUN_POSTGRES_TESTS=1 to run PostgreSQL integration tests",
)


@pytest_asyncio.fixture
async def session_factory():
    test_engine = create_async_engine(get_settings().postgres_dsn, poolclass=NullPool)
    factory = async_sessionmaker(test_engine, expire_on_commit=False)
    yield factory
    await test_engine.dispose()


@pytest.mark.asyncio
async def test_job_success_state_machine(session_factory) -> None:
    job_id = uuid4()
    try:
        async with session_factory() as session:
            created = await create_job(
                session,
                job_id=job_id,
                filename="demo.mp4",
                source_key=f"uploads/{job_id}/demo.mp4",
            )
            assert created.status == JobStatus.QUEUED.value

            processing = await mark_processing(session, job_id)
            assert processing.status == JobStatus.PROCESSING.value

            done = await mark_done(session, job_id, output_key=f"outputs/{job_id}/demo.mp4")
            assert done.status == JobStatus.DONE.value
            assert done.output_key == f"outputs/{job_id}/demo.mp4"
            assert done.error is None

            with pytest.raises(InvalidJobTransitionError):
                await mark_processing(session, job_id)
    finally:
        async with session_factory() as cleanup_session:
            await cleanup_session.execute(delete(Job).where(Job.id == job_id))
            await cleanup_session.commit()


@pytest.mark.asyncio
async def test_failed_job_requires_readable_error(session_factory) -> None:
    job_id = uuid4()
    try:
        async with session_factory() as session:
            await create_job(
                session,
                job_id=job_id,
                filename="broken.mp4",
                source_key=f"uploads/{job_id}/broken.mp4",
            )
            await mark_processing(session, job_id)
            failed = await mark_failed(session, job_id, error="source object could not be read")

            assert failed.status == JobStatus.FAILED.value
            assert failed.error == "source object could not be read"
            assert failed.output_key is None

            loaded = await get_job(session, job_id)
            assert loaded is not None
            assert loaded.status == JobStatus.FAILED.value
    finally:
        async with session_factory() as cleanup_session:
            await cleanup_session.execute(delete(Job).where(Job.id == job_id))
            await cleanup_session.commit()


@pytest.mark.asyncio
async def test_empty_failure_message_is_rejected(session_factory) -> None:
    job_id = uuid4()
    try:
        async with session_factory() as session:
            await create_job(
                session,
                job_id=job_id,
                filename="broken.mp4",
                source_key=f"uploads/{job_id}/broken.mp4",
            )
            await mark_processing(session, job_id)

            with pytest.raises(ValueError, match="readable error"):
                await mark_failed(session, job_id, error="   ")
    finally:
        async with session_factory() as cleanup_session:
            await cleanup_session.execute(delete(Job).where(Job.id == job_id))
            await cleanup_session.commit()
