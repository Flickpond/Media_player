from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest

from app.models.job import JobStatus
from app.worker import reaper


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
        worker_job_timeout_seconds=900, reaper_lease_seconds=1800, reaper_orphan_grace_seconds=3600
    ))
    monkeypatch.setattr(reaper, "get_session_factory", lambda: sessions)
    monkeypatch.setattr(reaper, "list_stale", list_stale)
    monkeypatch.setattr(reaper, "list_source_keys", list_source_keys)
    monkeypatch.setattr(reaper, "get_storage_service", lambda: storage)
    monkeypatch.setattr(reaper, "get_redis_connection", lambda: object())

    assert await reaper.run_once() == (0, 0, 1)
    assert storage.deleted == [orphan]


@pytest.mark.asyncio
async def test_reaper_marks_stale_processing_job_failed(monkeypatch):
    job = SimpleNamespace(id="job-1", source_key="uploads/job-1/video.mp4")
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
        worker_job_timeout_seconds=900, reaper_lease_seconds=1800, reaper_orphan_grace_seconds=3600
    ))
    monkeypatch.setattr(reaper, "get_session_factory", lambda: sessions)
    monkeypatch.setattr(reaper, "list_stale", list_stale)
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
        worker_job_timeout_seconds=900, reaper_lease_seconds=1800, reaper_orphan_grace_seconds=3600
    ))
    monkeypatch.setattr(reaper, "get_session_factory", lambda: sessions)
    monkeypatch.setattr(reaper, "list_stale", list_stale)
    monkeypatch.setattr(reaper, "list_source_keys", list_source_keys)
    monkeypatch.setattr(reaper, "get_storage_service", lambda: SimpleNamespace(
        list_objects=list_objects
    ))
    monkeypatch.setattr(reaper, "get_redis_connection", lambda: object())
    monkeypatch.setattr(reaper.RqJob, "exists", lambda *_args, **_kwargs: False)
    monkeypatch.setattr(reaper, "enqueue_job", lambda job_id: enqueued.append(job_id))

    assert await reaper.run_once() == (0, 1, 0)
    assert enqueued == ["job-2"]
