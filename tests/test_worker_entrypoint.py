"""The worker entrypoint and the dependency wiring behind it.

These paths only run when a real worker process starts, so nothing else in the
suite touches them -- which is exactly why a typo here would survive until
`docker compose up`.
"""

import logging
import os
from uuid import uuid4

import fakeredis
import pytest
from rq import Queue, SimpleWorker, Worker

from app import queue as queue_module
from app.worker import __main__ as entrypoint
from app.worker import db as worker_db
from app.worker import storage, tasks
from app.worker.storage import CopyProcessor, MinioObjectStore


@pytest.fixture
def fake_connection(monkeypatch: pytest.MonkeyPatch):
    connection = fakeredis.FakeStrictRedis()
    monkeypatch.setattr(entrypoint, "get_redis_connection", lambda: connection)
    return connection


def test_worker_listens_on_the_configured_queue(fake_connection):
    worker = entrypoint.build_worker()

    assert [q.name for q in worker.queues] == ["video_jobs"]


def test_windows_falls_back_to_the_in_process_worker(fake_connection):
    """No fork on Windows; a teammate there must still be able to run it."""
    worker = entrypoint.build_worker(burst_safe=True)

    assert isinstance(worker, SimpleWorker)


def test_containers_use_the_forking_worker(fake_connection):
    if not hasattr(os, "fork"):
        pytest.skip("this host has no fork, so the forking path cannot be built here")

    assert type(entrypoint.build_worker()) is Worker


def test_configure_logging_sets_the_requested_level():
    entrypoint.configure_logging("warning")

    assert logging.getLogger().level == logging.WARNING


def test_unknown_log_level_falls_back_to_info():
    entrypoint.configure_logging("not-a-level")

    assert logging.getLogger().level == logging.INFO


def test_main_runs_the_worker_in_burst_mode(monkeypatch: pytest.MonkeyPatch):
    calls = {}

    class FakeWorker:
        def work(self, burst: bool = False) -> None:
            calls["burst"] = burst

    monkeypatch.setattr(entrypoint, "build_worker", lambda **_: FakeWorker())
    monkeypatch.setattr("sys.argv", ["app.worker", "--burst", "--log-level", "warning"])

    entrypoint.main()

    assert calls == {"burst": True}


def test_main_defaults_to_a_long_running_worker(monkeypatch: pytest.MonkeyPatch):
    calls = {}

    class FakeWorker:
        def work(self, burst: bool = False) -> None:
            calls["burst"] = burst

    monkeypatch.setattr(entrypoint, "build_worker", lambda **_: FakeWorker())
    monkeypatch.setattr("sys.argv", ["app.worker"])

    entrypoint.main()

    assert calls == {"burst": False}


# --- wiring the real dependencies -----------------------------------------


def test_redis_connection_is_built_from_the_configured_url(monkeypatch: pytest.MonkeyPatch):
    queue_module.get_redis_connection.cache_clear()
    captured = {}

    class FakeRedis:
        @classmethod
        def from_url(cls, url: str):
            captured["url"] = url
            return "connection"

    monkeypatch.setattr(queue_module, "Redis", FakeRedis)
    try:
        queue_module.get_redis_connection()
    finally:
        queue_module.get_redis_connection.cache_clear()

    assert captured["url"] == "redis://127.0.0.1:6379/0"


def test_get_queue_uses_the_shared_queue_name_and_timeout():
    connection = fakeredis.FakeStrictRedis()

    built = queue_module.get_queue(connection)

    assert isinstance(built, Queue)
    assert built.name == "video_jobs"
    assert built._default_timeout == 900


def test_worker_session_factory_avoids_connection_pooling():
    """Pooled connections break across the per-task asyncio.run boundary."""
    worker_db.get_worker_session_factory.cache_clear()
    try:
        factory = worker_db.get_worker_session_factory()
        engine = factory.kw["bind"]

        assert engine.pool.__class__.__name__ == "NullPool"
    finally:
        worker_db.get_worker_session_factory.cache_clear()


def test_processing_step_is_a_copy_processor_over_minio():
    storage.get_processing_step.cache_clear()
    try:
        step = storage.get_processing_step()
    finally:
        storage.get_processing_step.cache_clear()

    assert isinstance(step, CopyProcessor)
    assert isinstance(step._store, MinioObjectStore)
    assert step.output_key_for(job_id=(j := uuid4()), source_key="uploads/a/clip.mp4") == (
        f"outputs/{j}/clip.mp4"
    )


# --- the synchronous RQ entrypoint ----------------------------------------


def test_process_job_bridges_rq_to_the_async_state_machine(monkeypatch: pytest.MonkeyPatch):
    """RQ calls a sync function; the state machine is async. Cover the bridge."""
    seen = {}

    async def fake_async(job_id, *, session_factory, step):
        seen["job_id"] = job_id
        return tasks.JobOutcome.DONE

    monkeypatch.setattr(tasks, "process_job_async", fake_async)
    monkeypatch.setattr(tasks, "get_worker_session_factory", lambda: "factory")
    monkeypatch.setattr(tasks, "get_processing_step", lambda: "step")
    job_id = uuid4()

    result = tasks.process_job(str(job_id))

    assert result == "done"
    assert seen["job_id"] == job_id


def test_process_job_rejects_a_malformed_job_id(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(tasks, "get_worker_session_factory", lambda: "factory")
    monkeypatch.setattr(tasks, "get_processing_step", lambda: "step")

    with pytest.raises(ValueError):
        tasks.process_job("not-a-uuid")
