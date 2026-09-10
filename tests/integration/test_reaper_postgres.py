"""The reaper's data-touching paths, against real PostgreSQL and MinIO.

These deliberately do **not** call `run_once()`.

`run_once()` sweeps the whole jobs table and the entire `uploads/` prefix, and
deletes what it judges orphaned. Running that against a shared database would
reap other people's in-flight work and delete their objects -- and this suite
is pointed at whatever `POSTGRES_DSN` says, which on a developer machine is the
same stack they are using. A test that is destructive to everything but itself
is not a test worth having.

So the orchestration stays covered by fakes in `tests/test_reaper.py`, and
these cover the pieces that actually talk to a service, scoped to rows and keys
this file created. Between them every line the reaper depends on is exercised
against something real.
"""

import io
import os
import uuid
from datetime import UTC, datetime, timedelta

import pytest
import pytest_asyncio
from sqlalchemy import delete, text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from app.config import get_settings
from app.models.job import Job, JobStatus
from app.repositories.jobs import create_job, get_job, list_stale, mark_stale_failed
from app.services.storage import get_storage_service

pytestmark = pytest.mark.skipif(
    os.getenv("RUN_POSTGRES_TESTS") != "1",
    reason="set RUN_POSTGRES_TESTS=1 to run tests that need the live compose stack",
)


@pytest_asyncio.fixture
async def session_factory(owner):
    engine = create_async_engine(get_settings().postgres_dsn, poolclass=NullPool)
    yield async_sessionmaker(engine, expire_on_commit=False)
    await engine.dispose()


async def _stale_processing_job(session_factory, *, age_seconds: int, owner) -> uuid.UUID:
    """A row sitting in `processing`, last touched `age_seconds` ago."""
    job_id = uuid.uuid4()
    async with session_factory() as session:
        await create_job(
            session,
            owner_id=owner.id,
            filename="stranded.mp4",
            source_key=f"uploads/{job_id}/stranded.mp4",
            job_id=job_id,
        )
        await session.execute(
            text(
                "update jobs set status = 'processing', "
                "updated_at = now() - make_interval(secs => :age) where id = :id"
            ),
            {"age": age_seconds, "id": str(job_id)},
        )
        await session.commit()
    return job_id


async def _drop(session_factory, job_id: uuid.UUID) -> None:
    async with session_factory() as session:
        await session.execute(delete(Job).where(Job.id == job_id))
        await session.commit()


# --- the reaping path -----------------------------------------------------


@pytest.mark.asyncio
async def test_a_stranded_processing_row_is_failed_with_a_readable_error(session_factory):
    job_id = await _stale_processing_job(session_factory, age_seconds=7200)
    try:
        async with session_factory() as session:
            reaped = await mark_stale_failed(session, job_id)

        assert reaped is not None
        async with session_factory() as session:
            job = await get_job(session, job_id)

        assert job.status == JobStatus.FAILED.value
        # This string reaches the user through GET /jobs/{id}, so it has to read
        # like something a person can act on rather than an exception class.
        assert job.error == "worker stopped responding; job was not completed"
    finally:
        await _drop(session_factory, job_id)


@pytest.mark.asyncio
async def test_two_reapers_racing_cannot_both_claim_the_same_row(session_factory):
    """The conditional update is the only thing preventing a double transition."""
    job_id = await _stale_processing_job(session_factory, age_seconds=7200)
    try:
        async with session_factory() as session:
            first = await mark_stale_failed(session, job_id)
        async with session_factory() as session:
            second = await mark_stale_failed(session, job_id)

        assert first is not None, "the first reaper should win"
        assert second is None, "the second reaper must find nothing left to claim"
    finally:
        await _drop(session_factory, job_id)


@pytest.mark.asyncio
async def test_list_stale_respects_the_cutoff(session_factory):
    """A row inside its lease must not be offered up for reaping."""
    old = await _stale_processing_job(session_factory, age_seconds=7200)
    fresh = await _stale_processing_job(session_factory, age_seconds=5)
    try:
        cutoff = datetime.now(UTC) - timedelta(seconds=3600)
        async with session_factory() as session:
            stale = await list_stale(session, status=JobStatus.PROCESSING, before=cutoff)

        ids = {job.id for job in stale}
        # Membership, not counts: this table is shared with a running stack.
        assert old in ids
        assert fresh not in ids
    finally:
        await _drop(session_factory, old)
        await _drop(session_factory, fresh)


# --- the object-store path ------------------------------------------------


@pytest.mark.asyncio
async def test_list_objects_returns_real_keys_and_timestamps():
    """Regression guard for the listing that silently did nothing.

    `list_objects` defaulted to the SDK's `recursive=False`, which returns
    pseudo-directory entries like `uploads/<uuid>/` with `last_modified=None`.
    The reaper skips anything without a timestamp, so orphan cleanup was a
    permanent no-op that reported success. Assert on both halves: a real key,
    and a usable timestamp.
    """
    storage = get_storage_service()
    await storage.ensure_bucket()
    key = f"uploads/{uuid.uuid4()}/probe.mp4"
    await storage.upload_stream(key, io.BytesIO(b"probe"), length=5)
    try:
        found = {k: m for k, m in await storage.list_objects(prefix="uploads/")}

        assert key in found, "recursive listing must return the object, not its prefix"
        assert found[key] is not None, "a missing timestamp makes the reaper skip it forever"
        assert not any(k.endswith("/") for k in found), "no pseudo-directory entries"
    finally:
        await storage.delete_object(key)


@pytest.mark.asyncio
async def test_delete_object_removes_it():
    storage = get_storage_service()
    await storage.ensure_bucket()
    key = f"uploads/{uuid.uuid4()}/doomed.mp4"
    await storage.upload_stream(key, io.BytesIO(b"doomed"), length=6)

    await storage.delete_object(key)

    remaining = {k for k, _ in await storage.list_objects(prefix="uploads/")}
    assert key not in remaining
