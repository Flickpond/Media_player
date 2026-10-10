from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest

from app.models.job import JobStatus
from app.worker import reaper


async def _no_stale_ladders(_session, *, before):
    return []


@pytest.mark.asyncio
async def test_reaper_deletes_only_old_orphan_objects(monkeypatch):
    now = datetime.now(UTC)
    orphan = "uploads/orphan/file.mp4"
    fresh = "uploads/in-flight/file.mp4"
    storage = SimpleNamespace(deleted=[])

    async def list_objects(prefix):
        assert prefix == "uploads/"
        return [(orphan, now - timedelta(hours=2)), (fresh, now)]

    async def delete_object(key):
        storage.deleted.append(key)

    storage.list_objects = list_objects
    storage.delete_object = delete_object

    async def list_stale(*_args, **_kwargs):
        return []

    async def list_source_keys(_session):
        return {fresh}

    @asynccontextmanager
    async def sessions():
        yield object()

    monkeypatch.setattr(reaper, "get_settings", lambda: SimpleNamespace(
        worker_job_timeout_seconds=900, reaper_lease_seconds=1800, reaper_abandoned_seconds=300,
        reaper_orphan_grace_seconds=3600
    ))
    monkeypatch.setattr(reaper, "get_session_factory", lambda: sessions)
    monkeypatch.setattr(reaper, "list_stale", list_stale)
    monkeypatch.setattr(reaper, "list_stale_ladders", _no_stale_ladders)
    monkeypatch.setattr(reaper, "list_source_keys", list_source_keys)
    monkeypatch.setattr(reaper, "get_storage_service", lambda: storage)
    monkeypatch.setattr(reaper, "get_redis_connection", lambda: object())

    assert await reaper.run_once() == (0, 0, 1)
    assert storage.deleted == [orphan]


@pytest.mark.asyncio
async def test_reaper_marks_stale_processing_job_failed(monkeypatch):
    # Older than the lease: reaped whatever its heartbeat says.
    job = SimpleNamespace(
        id="job-1",
        source_key="uploads/job-1/video.mp4",
        updated_at=datetime.now(UTC) - timedelta(hours=2),
    )
    marked = []

    async def list_stale(_session, *, status, before):
        return [job] if status is JobStatus.PROCESSING else []

    async def mark_stale_failed(_session, job_id):
        marked.append(job_id)
        return job

    async def list_source_keys(_session):
        return {job.source_key}

    @asynccontextmanager
    async def sessions():
        yield object()

    async def list_objects(prefix):
        return []

    monkeypatch.setattr(reaper, "get_settings", lambda: SimpleNamespace(
        worker_job_timeout_seconds=900, reaper_lease_seconds=1800, reaper_abandoned_seconds=300,
        reaper_orphan_grace_seconds=3600
    ))
    monkeypatch.setattr(reaper, "get_session_factory", lambda: sessions)
    monkeypatch.setattr(reaper, "list_stale", list_stale)
    monkeypatch.setattr(reaper, "list_stale_ladders", _no_stale_ladders)
    monkeypatch.setattr(reaper, "mark_stale_failed", mark_stale_failed)
    monkeypatch.setattr(reaper, "list_source_keys", list_source_keys)
    monkeypatch.setattr(reaper, "get_storage_service", lambda: SimpleNamespace(
        list_objects=list_objects
    ))
    monkeypatch.setattr(reaper, "get_redis_connection", lambda: object())

    assert await reaper.run_once() == (1, 0, 0)
    assert marked == ["job-1"]


@pytest.mark.asyncio
async def test_reaper_requeues_stale_queued_job_missing_from_rq(monkeypatch):
    job = SimpleNamespace(id="job-2", source_key="uploads/job-2/video.mp4")
    enqueued = []

    async def list_stale(_session, *, status, before):
        return [job] if status is JobStatus.QUEUED else []

    async def list_source_keys(_session):
        return {job.source_key}

    @asynccontextmanager
    async def sessions():
        yield object()

    async def list_objects(prefix):
        return []

    monkeypatch.setattr(reaper, "get_settings", lambda: SimpleNamespace(
        worker_job_timeout_seconds=900, reaper_lease_seconds=1800, reaper_abandoned_seconds=300,
        reaper_orphan_grace_seconds=3600
    ))
    monkeypatch.setattr(reaper, "get_session_factory", lambda: sessions)
    monkeypatch.setattr(reaper, "list_stale", list_stale)
    monkeypatch.setattr(reaper, "list_stale_ladders", _no_stale_ladders)
    monkeypatch.setattr(reaper, "list_source_keys", list_source_keys)
    monkeypatch.setattr(reaper, "get_storage_service", lambda: SimpleNamespace(
        list_objects=list_objects
    ))
    monkeypatch.setattr(reaper, "get_redis_connection", lambda: object())
    monkeypatch.setattr(reaper.RqJob, "exists", lambda *_args, **_kwargs: False)
    monkeypatch.setattr(reaper, "enqueue_job", lambda job_id: enqueued.append(job_id))

    assert await reaper.run_once() == (0, 1, 0)
    assert enqueued == ["job-2"]


# --- a dead worker is found by its missing heartbeat, not the lease -----------


def _registry_with(connection, member: str, expires_in: float):
    """A started-registry entry as RQ writes it: `{job_id}:{execution_id}`,
    scored with its expiry."""
    from rq import Queue
    from rq.registry import StartedJobRegistry

    queue = Queue("video_jobs", connection=connection)
    key = StartedJobRegistry(queue=queue).key
    connection.zadd(key, {member: datetime.now(UTC).timestamp() + expires_in})
    return queue


def test_a_job_whose_worker_keeps_its_heartbeat_is_alive():
    import fakeredis

    connection = fakeredis.FakeStrictRedis()
    queue = _registry_with(connection, "job-1:exec-a", expires_in=60)

    assert reaper._heartbeat_alive("job-1", queue, connection)
    assert not reaper._heartbeat_alive("job-2", queue, connection)


def test_a_job_whose_heartbeat_lapsed_is_dead():
    import fakeredis

    connection = fakeredis.FakeStrictRedis()
    queue = _registry_with(connection, "job-1:exec-a", expires_in=-30)

    assert not reaper._heartbeat_alive("job-1", queue, connection)


def test_a_job_id_that_only_shares_a_prefix_is_not_mistaken_for_it():
    import fakeredis

    connection = fakeredis.FakeStrictRedis()
    queue = _registry_with(connection, "job-10:exec-a", expires_in=60)

    assert not reaper._heartbeat_alive("job-1", queue, connection)


@pytest.fixture
def processing_env(monkeypatch):
    state = SimpleNamespace(jobs=[], alive=set(), failed=[])

    async def list_stale(_session, *, status, before):
        return state.jobs if status is JobStatus.PROCESSING else []

    async def mark_stale_failed(_session, job_id):
        state.failed.append(job_id)
        return object()

    async def nothing(*_args, **_kwargs):
        return []

    @asynccontextmanager
    async def sessions():
        yield object()

    async def no_keys(_session):
        return set()

    monkeypatch.setattr(reaper, "get_settings", lambda: SimpleNamespace(
        reaper_lease_seconds=1800, reaper_abandoned_seconds=300,
        reaper_orphan_grace_seconds=3600
    ))
    monkeypatch.setattr(reaper, "get_session_factory", lambda: sessions)
    monkeypatch.setattr(reaper, "list_stale", list_stale)
    monkeypatch.setattr(reaper, "list_stale_ladders", nothing)
    monkeypatch.setattr(reaper, "mark_stale_failed", mark_stale_failed)
    monkeypatch.setattr(reaper, "list_source_keys", no_keys)
    storage = SimpleNamespace(list_objects=nothing)
    monkeypatch.setattr(reaper, "get_storage_service", lambda: storage)
    monkeypatch.setattr(reaper, "get_redis_connection", lambda: object())
    monkeypatch.setattr(reaper, "get_queue", lambda _c: "video_jobs")
    monkeypatch.setattr(reaper, "get_ladder_queue", lambda _c: "video_jobs-ladder")
    monkeypatch.setattr(
        reaper, "_heartbeat_alive", lambda job_id, _queue, _conn: job_id in state.alive
    )
    return state


def _processing(minutes_ago: int):
    return SimpleNamespace(
        id=f"job-{minutes_ago}", updated_at=datetime.now(UTC) - timedelta(minutes=minutes_ago)
    )


@pytest.mark.asyncio
async def test_a_long_encode_on_a_live_worker_is_left_alone(processing_env):
    job = _processing(10)
    processing_env.jobs = [job]
    processing_env.alive = {job.id}

    assert await reaper.run_once() == (0, 0, 0)


@pytest.mark.asyncio
async def test_a_job_whose_worker_died_is_failed_in_minutes_not_after_the_lease(processing_env):
    """10 minutes in, against a 30-minute lease: found by the heartbeat."""
    job = _processing(10)
    processing_env.jobs = [job]

    assert await reaper.run_once() == (1, 0, 0)
    assert processing_env.failed == [job.id]


@pytest.mark.asyncio
async def test_a_live_worker_stuck_past_the_lease_is_still_reaped(processing_env):
    job = _processing(45)
    processing_env.jobs = [job]
    processing_env.alive = {job.id}

    assert await reaper.run_once() == (1, 0, 0)
