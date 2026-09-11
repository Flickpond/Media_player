import os
from uuid import uuid4

import pytest
import pytest_asyncio
from sqlalchemy import delete
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from app.config import get_settings
from app.models.job import Job, JobStatus
from app.models.user import User
from app.repositories.jobs import (
    InvalidJobTransitionError,
    JobNotFoundError,
    create_job,
    delete_job,
    get_job,
    list_jobs,
    mark_done,
    mark_failed,
    mark_processing,
)
from app.repositories.users import create_user

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
async def test_job_success_state_machine(session_factory, owner) -> None:
    job_id = uuid4()
    try:
        async with session_factory() as session:
            created = await create_job(
                session,
                owner_id=owner.id,
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
async def test_failed_job_requires_readable_error(session_factory, owner) -> None:
    job_id = uuid4()
    try:
        async with session_factory() as session:
            await create_job(
                session,
                owner_id=owner.id,
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
async def test_empty_failure_message_is_rejected(session_factory, owner) -> None:
    job_id = uuid4()
    try:
        async with session_factory() as session:
            await create_job(
                session,
                owner_id=owner.id,
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


@pytest.mark.asyncio
async def test_list_jobs_pages_without_repeating_or_dropping_rows(session_factory, owner) -> None:
    """Real SQL, because LIMIT/OFFSET and the tie-break are the whole point.

    All five rows are created in a tight loop, so `created_at` values can be
    identical to the microsecond -- exactly the case where ordering on
    `created_at` alone lets a row appear on two pages, or on none.
    """
    made = []
    try:
        async with session_factory() as session:
            for index in range(5):
                job = await create_job(
                    session,
                    owner_id=owner.id,
                    filename=f"page-{index}.mp4",
                    source_key=f"uploads/page-{index}.mp4",
                    job_id=uuid4(),
                )
                made.append(job.id)

        # Page through the whole table rather than assuming these five rows are
        # the only ones in it -- this database is shared with the live stack.
        seen: list = []
        async with session_factory() as session:
            offset = 0
            while offset < 1000:  # a bound, so a paging bug cannot spin forever
                page = await list_jobs(session, limit=2, offset=offset)
                if not page:
                    break
                assert len(page) <= 2, "a page came back larger than its limit"
                seen.extend(job.id for job in page)
                offset += 2

        assert len(seen) == len(set(seen)), "a row was returned on two pages"
        assert set(made) <= set(seen), "a row was never returned at all"
    finally:
        async with session_factory() as cleanup:
            await cleanup.execute(delete(Job).where(Job.id.in_(made)))
            await cleanup.commit()


@pytest.mark.asyncio
async def test_list_jobs_returns_newest_first(session_factory, owner) -> None:
    made = []
    try:
        async with session_factory() as session:
            for index in range(3):
                job = await create_job(
                    session,
                    owner_id=owner.id,
                    filename=f"order-{index}.mp4",
                    source_key=f"uploads/order-{index}.mp4",
                    job_id=uuid4(),
                )
                made.append(job)

        async with session_factory() as session:
            listed = await list_jobs(session, limit=50, offset=0)

        timestamps = [job.created_at for job in listed]
        assert timestamps == sorted(timestamps, reverse=True)
    finally:
        async with session_factory() as cleanup:
            await cleanup.execute(delete(Job).where(Job.id.in_([job.id for job in made])))
            await cleanup.commit()


# --- DELETE /jobs/{id}: an accidental upload, or general library cleanup ---


@pytest.mark.asyncio
async def test_delete_job_removes_the_row_and_returns_both_storage_keys(
    session_factory, owner
) -> None:
    """The API cleans up storage from what this returns -- both keys have to
    travel back, not just enough to prove the row is gone.
    """
    job_id = uuid4()
    async with session_factory() as session:
        await create_job(
            session,
            owner_id=owner.id,
            job_id=job_id,
            filename="demo.mp4",
            source_key=f"uploads/{job_id}/demo.mp4",
        )
        await mark_processing(session, job_id)
        await mark_done(session, job_id, output_key=f"outputs/{job_id}/demo.mp4")

    async with session_factory() as session:
        deleted = await delete_job(session, job_id, owner_id=owner.id)

    assert deleted is not None
    assert deleted.source_key == f"uploads/{job_id}/demo.mp4"
    assert deleted.output_key == f"outputs/{job_id}/demo.mp4"

    async with session_factory() as session:
        assert await get_job(session, job_id) is None


@pytest.mark.asyncio
async def test_delete_job_is_scoped_to_the_owner(session_factory, owner) -> None:
    """Someone else's id must not be able to delete this job -- and, same as
    get_job, the row must still be there afterward, not just "not deleted by
    that caller".
    """
    job_id = uuid4()
    async with session_factory() as session:
        stranger = await create_user(
            session, email=f"stranger-{uuid4().hex[:10]}@example.test", password_hash="x"
        )
    try:
        async with session_factory() as session:
            await create_job(
                session,
                owner_id=owner.id,
                job_id=job_id,
                filename="demo.mp4",
                source_key=f"uploads/{job_id}/demo.mp4",
            )

        async with session_factory() as session:
            result = await delete_job(session, job_id, owner_id=stranger.id)
        assert result is None

        async with session_factory() as session:
            assert await get_job(session, job_id, owner_id=owner.id) is not None
    finally:
        async with session_factory() as cleanup:
            await cleanup.execute(delete(Job).where(Job.id == job_id))
            await cleanup.execute(delete(User).where(User.id == stranger.id))
            await cleanup.commit()


@pytest.mark.asyncio
async def test_deleting_a_processing_job_leaves_the_worker_to_find_it_gone(
    session_factory, owner
) -> None:
    """The safety argument `delete_job`'s docstring makes, proven: a worker
    mid-flight on a job that gets deleted loses its conditional update to a
    row that is no longer there, the same as any other race this state
    machine already tolerates -- it does not corrupt one that is.
    """
    job_id = uuid4()
    async with session_factory() as session:
        await create_job(
            session,
            owner_id=owner.id,
            job_id=job_id,
            filename="demo.mp4",
            source_key=f"uploads/{job_id}/demo.mp4",
        )
        await mark_processing(session, job_id)

    async with session_factory() as session:
        deleted = await delete_job(session, job_id, owner_id=owner.id)
    assert deleted is not None

    async with session_factory() as session:
        with pytest.raises(JobNotFoundError):
            await mark_done(session, job_id, output_key=f"outputs/{job_id}/demo.mp4")
