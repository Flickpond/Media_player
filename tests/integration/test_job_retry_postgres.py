"""Prove retry ownership, transaction rollback and races on real PostgreSQL."""

import asyncio
import os
from datetime import UTC, datetime
from functools import partial
from unittest.mock import Mock
from uuid import UUID, uuid4

import fakeredis
import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from redis.exceptions import ConnectionError as RedisConnectionError
from rq import Queue, SimpleWorker
from sqlalchemy import update
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from app.api import jobs as jobs_api
from app.config import get_settings
from app.database import get_session
from app.main import create_app
from app.models.job import Job, JobStatus
from app.queue import enqueue_job
from app.repositories.jobs import (
    create_job,
    get_job,
    mark_done,
    mark_failed,
    mark_processing,
    prepare_retry,
)
from app.worker import tasks
from app.worker.probe import SourceProbe
from app.worker.storage import ObjectStoreError
from app.worker.tasks import JobOutcome, process_job_async
from app.worker.validation import validate_clip, validate_crop
from tests.conftest import authenticate_as

pytestmark = pytest.mark.skipif(
    os.getenv("RUN_POSTGRES_TESTS") != "1",
    reason="set RUN_POSTGRES_TESTS=1 to run PostgreSQL integration tests",
)


@pytest_asyncio.fixture
async def session_factory():
    engine = create_async_engine(get_settings().postgres_dsn, poolclass=NullPool)
    yield async_sessionmaker(engine, expire_on_commit=False)
    await engine.dispose()


@pytest_asyncio.fixture
async def failed_job(session_factory, owner):
    job_id = uuid4()
    async with session_factory() as session:
        await create_job(
            session,
            owner_id=owner.id,
            job_id=job_id,
            filename="source.mp4",
            source_key=f"uploads/{job_id}/source.mp4",
        )
        await mark_processing(session, job_id)
        await mark_failed(session, job_id, error="temporary processing failure")
        # Fixtures may seed old rows directly. Production transitions must go
        # through the repository, whose behaviour is what these tests exercise.
        await session.execute(
            update(Job)
            .where(Job.id == job_id)
            .values(
                operations=[{"operation": "clip", "params": {"start": 0, "end": 1}}],
                hls_key=f"outputs/{job_id}/hls/master.m3u8",
                updated_at=datetime(2020, 1, 1, tzinfo=UTC),
            )
        )
        await session.commit()
    # The shared owner fixture cleans up only this test's rows.
    return job_id


@pytest_asyncio.fixture
async def client_for(session_factory, monkeypatch):
    enqueued = []
    monkeypatch.setattr(jobs_api, "enqueue_job", enqueued.append)
    clients = []

    async def build(user):
        app = create_app()

        async def database():
            async with session_factory() as session:
                yield session

        app.dependency_overrides[get_session] = database
        authenticate_as(app, user)
        client = AsyncClient(transport=ASGITransport(app=app), base_url="http://test")
        clients.append(client)
        return client

    yield build, enqueued
    for client in clients:
        await client.aclose()


async def test_retry_preserves_the_source_operations_and_identity_but_clears_old_results(
    session_factory, failed_job, owner, client_for
):
    build, enqueued = client_for
    async with session_factory() as session:
        before = await get_job(session, failed_job)
    client = await build(owner)
    response = await client.post(f"/jobs/{failed_job}/retry")
    assert response.status_code == 202
    assert response.json() == {"job_id": str(failed_job)}
    assert enqueued == [failed_job]
    async with session_factory() as session:
        after = await get_job(session, failed_job)
    for field in ("id", "owner_id", "filename", "source_key", "operations", "created_at"):
        assert getattr(after, field) == getattr(before, field)
    assert after.status == "queued"
    assert after.error is after.output_key is after.hls_key is None
    assert after.updated_at > before.updated_at


async def test_two_concurrent_http_retries_enqueue_exactly_once(client_for, failed_job, owner):
    build, enqueued = client_for
    clients = [await build(owner), await build(owner)]
    replies = await asyncio.gather(
        *(client.post(f"/jobs/{failed_job}/retry") for client in clients)
    )
    assert sorted(reply.status_code for reply in replies) == [202, 409]
    assert enqueued == [failed_job]


@pytest.mark.parametrize("current", [JobStatus.QUEUED, JobStatus.PROCESSING, JobStatus.DONE])
async def test_only_failed_jobs_are_retryable(session_factory, owner, client_for, current):
    build, enqueued = client_for
    async with session_factory() as session:
        job = await create_job(
            session, owner_id=owner.id, filename="source.mp4", source_key="uploads/test.mp4"
        )
        if current in {JobStatus.PROCESSING, JobStatus.DONE}:
            await mark_processing(session, job.id)
        if current == JobStatus.DONE:
            await mark_done(session, job.id, output_key=f"outputs/{job.id}/result.mp4")
    client = await build(owner)
    response = await client.post(f"/jobs/{job.id}/retry")
    assert response.status_code == 409
    assert not enqueued
    async with session_factory() as session:
        assert (await get_job(session, job.id)).status == current.value


async def test_foreign_and_unknown_ids_are_indistinguishable_even_for_an_operator(
    client_for, failed_job, other_user, operator, session_factory
):
    build, enqueued = client_for
    for caller in (other_user, operator):
        client = await build(caller)
        foreign = await client.post(f"/jobs/{failed_job}/retry")
        unknown = await client.post(f"/jobs/{uuid4()}/retry")
        assert foreign.status_code == unknown.status_code == 404
        assert foreign.json() == unknown.json() == {"error": "not found"}
    assert not enqueued
    async with session_factory() as session:
        assert (await get_job(session, failed_job)).status == "failed"


async def test_queue_outage_restores_the_original_row_and_a_later_retry_succeeds(
    session_factory, failed_job, owner, client_for, monkeypatch
):
    build, enqueued = client_for
    async with session_factory() as session:
        before = await get_job(session, failed_job)
    client = await build(owner)
    unavailable = Mock(side_effect=RedisConnectionError("internal Redis address"))
    monkeypatch.setattr(jobs_api, "enqueue_job", unavailable)
    response = await client.post(f"/jobs/{failed_job}/retry")
    assert response.status_code == 503
    async with session_factory() as session:
        after = await get_job(session, failed_job)
    for field in ("status", "error", "output_key", "hls_key", "operations", "updated_at"):
        assert getattr(after, field) == getattr(before, field)
    monkeypatch.setattr(jobs_api, "enqueue_job", enqueued.append)
    assert (await client.post(f"/jobs/{failed_job}/retry")).status_code == 202
    assert enqueued == [failed_job]


async def test_a_fast_worker_waits_for_retry_to_commit(session_factory, failed_job, owner):
    async def claim():
        async with session_factory() as session:
            return await mark_processing(session, failed_job)

    async with session_factory() as session:
        await prepare_retry(session, failed_job, owner_id=owner.id)
        worker = asyncio.create_task(claim())
        try:
            done, _pending = await asyncio.wait({worker}, timeout=0.1)
            assert not done
            await session.commit()
            claimed = await asyncio.wait_for(worker, timeout=5)
            assert claimed.status == "processing"
        finally:
            if not worker.done():
                worker.cancel()
            await asyncio.gather(worker, return_exceptions=True)


async def test_a_failed_rq_delivery_can_retry_the_same_id_and_reach_done(
    session_factory, owner, client_for, monkeypatch
):
    build, _enqueued = client_for
    connection = fakeredis.FakeStrictRedis()
    queue = Queue(f"retry-{uuid4().hex}", connection=connection)
    attempts = []

    class Step:
        def run(self, *, job_id, source_key):
            attempts.append((job_id, source_key))
            if len(attempts) == 1:
                raise ObjectStoreError("temporary test fault", user_message="please retry")
            return f"outputs/{job_id}/result.mp4"

    step = Step()

    def consume(job_id):
        return asyncio.run(
            process_job_async(
                # A real worker resolves the id string back to UUID in this way.
                UUID(job_id),
                session_factory=session_factory,
                step=step,
            )
        ).value

    monkeypatch.setattr(tasks, "process_job", consume)
    monkeypatch.setattr(jobs_api, "enqueue_job", partial(enqueue_job, queue=queue))

    def drain():
        SimpleWorker([queue], connection=connection).work(burst=True)

    # RQ normally installs main-thread signal handlers. This test worker runs
    # in a thread so it can drive its own event loop while the HTTP test runs.
    # TimerDeathPenalty supplies the equivalent timeout without SIGALRM.
    monkeypatch.setattr(SimpleWorker, "_install_signal_handlers", lambda self: None)
    from rq.timeouts import TimerDeathPenalty

    monkeypatch.setattr(SimpleWorker, "death_penalty_class", TimerDeathPenalty)
    async with session_factory() as session:
        job = await create_job(
            session, owner_id=owner.id, filename="source.mp4", source_key="uploads/source.mp4"
        )
    enqueue_job(job.id, queue=queue)
    await asyncio.to_thread(drain)
    async with session_factory() as session:
        assert (await get_job(session, job.id)).status == "failed"
    client = await build(owner)
    assert (await client.post(f"/jobs/{job.id}/retry")).status_code == 202
    await asyncio.to_thread(drain)
    async with session_factory() as session:
        completed = await get_job(session, job.id)
    assert completed.status == "done"
    assert completed.output_key == f"outputs/{job.id}/result.mp4"
    assert attempts == [(job.id, job.source_key)] * 2


@pytest.mark.parametrize(
    ("validate", "params", "message"),
    [
        (
            validate_crop,
            {"x": 0, "y": 0, "w": 1921, "h": 1080},
            "crop must fit within the source video's frame",
        ),
        (
            validate_clip,
            {"start": 0, "end": 11},
            "clip end must not exceed the source video's duration",
        ),
    ],
)
async def test_bs_adapter_reports_a_bad_edit_through_the_worker_and_polling_api(
    session_factory, owner, client_for, validate, params, message
):
    """The real state machine and API around a test-only B processor adapter.

    B's /edit endpoint/processor is not in this baseline; this is explicit
    contract coverage, not a claim to have run that unmerged implementation.
    """
    ffmpeg = Mock()

    class ValidatingStep:
        def run(self, *, job_id, source_key):
            try:
                validate(params, SourceProbe(1920, 1080, 10))
            except ValueError as exc:
                raise ObjectStoreError("edit validation failed", user_message=str(exc)) from exc
            ffmpeg()
            return f"outputs/{job_id}/result.mp4"

    async with session_factory() as session:
        job = await create_job(
            session, owner_id=owner.id, filename="source.mp4", source_key="uploads/source.mp4"
        )
    assert (
        await process_job_async(job.id, session_factory=session_factory, step=ValidatingStep())
        == JobOutcome.FAILED
    )
    ffmpeg.assert_not_called()
    build, _enqueued = client_for
    client = await build(owner)
    response = await client.get(f"/jobs/{job.id}")
    assert response.status_code == 200
    assert response.json()["status"] == "failed"
    assert response.json()["error"] == message
