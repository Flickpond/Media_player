"""The HLS ladder as its own job.

The MP4 job marks the video done with its ladder `pending` and queues the
ladder; this job settles it `ready` or `unavailable`. Nothing here may ever
touch `status` -- the video was finished before the ladder was queued.
"""

from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from uuid import UUID, uuid4

import fakeredis
import pytest
from rq import Queue

from app.models.job import HlsStatus, JobStatus
from app.queue import BUILD_LADDER_TASK, enqueue_ladder, ladder_rq_job_id
from app.worker import reaper, tasks
from app.worker.hls import HlsLadderBuilder
from app.worker.storage import ObjectStoreError, ProcessingResult
from app.worker.tasks import JobOutcome, build_ladder_async, process_job_async


class LadderJobs:
    """The rows and the three repository calls the ladder path makes."""

    def __init__(self) -> None:
        self.jobs: dict[UUID, SimpleNamespace] = {}
        self.settled: list[tuple[UUID, HlsStatus]] = []

    def add(self, **fields) -> SimpleNamespace:
        job = SimpleNamespace(
            id=uuid4(),
            status=JobStatus.DONE.value,
            hls_status=HlsStatus.PENDING.value,
            hls_key=None,
            source_key="uploads/a/demo.mov",
            output_key="outputs/a/demo.mp4",
            operations=None,
            width=3840,
            height=2160,
            duration_seconds=12.5,
        )
        for name, value in fields.items():
            setattr(job, name, value)
        self.jobs[job.id] = job
        return job

    async def get_job(self, _session, job_id):
        return self.jobs.get(job_id)

    def _settle(self, job_id, status, hls_key):
        job = self.jobs.get(job_id)
        if job is None or job.status != "done" or job.hls_status != HlsStatus.PENDING.value:
            return None
        job.hls_status, job.hls_key = status.value, hls_key
        self.settled.append((job_id, status))
        return job

    async def mark_ladder_ready(self, _session, job_id, *, hls_key):
        return self._settle(job_id, HlsStatus.READY, hls_key)

    async def mark_ladder_unavailable(self, _session, job_id):
        return self._settle(job_id, HlsStatus.UNAVAILABLE, None)


class FakeLadderStep:
    def __init__(self, *, raises=None, on_run=None) -> None:
        self.raises = raises
        self.on_run = on_run
        self.runs: list[tuple[UUID, str, object]] = []
        self.discarded: list[UUID] = []

    def run(self, *, job_id, source_key, probe):
        self.runs.append((job_id, source_key, probe))
        if self.on_run is not None:
            self.on_run()
        if self.raises is not None:
            raise self.raises
        return f"outputs/{job_id}/hls/master.m3u8"

    def discard(self, job_id):
        self.discarded.append(job_id)


@pytest.fixture
def rows(monkeypatch) -> LadderJobs:
    fake = LadderJobs()
    monkeypatch.setattr(tasks, "get_job", fake.get_job)
    monkeypatch.setattr(tasks, "mark_ladder_ready", fake.mark_ladder_ready)
    monkeypatch.setattr(tasks, "mark_ladder_unavailable", fake.mark_ladder_unavailable)
    return fake


@pytest.fixture
def session_factory():
    @asynccontextmanager
    async def _factory():
        yield object()

    return _factory


# --- building -----------------------------------------------------------------


async def test_an_uploads_ladder_is_cut_from_the_original_at_its_stored_size(
    rows, session_factory
):
    """From the source, not the 720p MP4, or a 4K upload's ladder tops out at
    720p. Sized from the stored probe, so the file is not probed twice.
    """
    job = rows.add()
    step = FakeLadderStep()

    settled = await build_ladder_async(job.id, session_factory=session_factory, ladder_step=step)

    assert settled is HlsStatus.READY
    (_, ladder_input, probe) = step.runs[0]
    assert ladder_input == "uploads/a/demo.mov"
    assert (probe.width, probe.height) == (3840, 2160)
    assert job.hls_key == f"outputs/{job.id}/hls/master.m3u8"
    assert job.status == "done"


async def test_an_edits_ladder_is_cut_from_the_edit(rows, session_factory):
    job = rows.add(operations=[{"operation": "clip"}], output_key="outputs/b/edit.mp4")
    step = FakeLadderStep()

    await build_ladder_async(job.id, session_factory=session_factory, ladder_step=step)

    assert step.runs[0][1] == "outputs/b/edit.mp4"


async def test_a_failed_ladder_leaves_the_video_done_and_says_so(rows, session_factory):
    job = rows.add()
    step = FakeLadderStep(raises=ObjectStoreError("ffmpeg died", user_message="unusable"))

    settled = await build_ladder_async(job.id, session_factory=session_factory, ladder_step=step)

    assert settled is HlsStatus.UNAVAILABLE
    assert (job.status, job.hls_status, job.hls_key) == ("done", "unavailable", None)


async def test_a_job_with_no_stored_size_is_settled_without_running_ffmpeg(
    rows, session_factory
):
    job = rows.add(width=None, height=None)
    step = FakeLadderStep()

    settled = await build_ladder_async(job.id, session_factory=session_factory, ladder_step=step)

    assert settled is HlsStatus.UNAVAILABLE
    assert step.runs == []


@pytest.mark.parametrize(
    "fields",
    [
        {"hls_status": HlsStatus.READY.value},
        {"hls_status": HlsStatus.UNAVAILABLE.value},
        {"status": JobStatus.FAILED.value},
    ],
    ids=["already-ready", "already-given-up", "not-done"],
)
async def test_a_job_no_longer_waiting_for_a_ladder_is_left_alone(
    rows, session_factory, fields
):
    """A duplicate delivery, or one the reaper already settled."""
    job = rows.add(**fields)
    step = FakeLadderStep()

    settled = await build_ladder_async(job.id, session_factory=session_factory, ladder_step=step)

    assert settled is None
    assert step.runs == [] and rows.settled == []


async def test_a_deleted_job_drops_its_ladder_entry(rows, session_factory):
    step = FakeLadderStep()

    assert (
        await build_ladder_async(uuid4(), session_factory=session_factory, ladder_step=step)
        is None
    )
    assert step.runs == []


async def test_a_ladder_finished_after_its_job_was_deleted_is_removed_from_storage(
    rows, session_factory
):
    """The delete route finds segments through `hls_key`, which this run
    never got to write -- so nothing else would ever remove them.
    """
    job = rows.add()
    step = FakeLadderStep(on_run=lambda: rows.jobs.pop(job.id))

    settled = await build_ladder_async(job.id, session_factory=session_factory, ladder_step=step)

    assert settled is None
    assert step.discarded == [job.id]


async def test_a_failed_cleanup_is_logged_not_raised(rows, session_factory, caplog):
    job = rows.add()
    step = FakeLadderStep(on_run=lambda: rows.jobs.pop(job.id))

    def exploding_discard(_job_id):
        raise ObjectStoreError("minio down", user_message="unused")

    step.discard = exploding_discard

    assert (
        await build_ladder_async(job.id, session_factory=session_factory, ladder_step=step)
        is None
    )
    assert "could not discard" in caplog.text


# --- the hand-off from the MP4 job ---------------------------------------------


class _Claimed(SimpleNamespace):
    pass


@pytest.fixture
def mp4_job(monkeypatch):
    """One queued job and the two transitions the MP4 path makes on it."""
    job = _Claimed(id=uuid4(), source_key="uploads/a/demo.mov", operations=None)
    done_with: dict = {}

    async def mark_processing(_session, job_id):
        return job

    async def mark_done(_session, job_id, **fields):
        done_with.update(fields)
        return job

    monkeypatch.setattr(tasks, "mark_processing", mark_processing)
    monkeypatch.setattr(tasks, "mark_done", mark_done)
    job.done_with = done_with
    return job


class _Step:
    def __init__(self, result):
        self.result = result

    def run(self, *, job_id, source_key):
        return self.result


async def test_the_video_is_done_with_its_ladder_pending_then_the_ladder_is_queued(
    mp4_job, session_factory
):
    queued: list[UUID] = []

    outcome = await process_job_async(
        mp4_job.id,
        session_factory=session_factory,
        step=_Step(ProcessingResult(output_key="outputs/a/demo.mp4", ladder_pending=True)),
        enqueue_ladder_fn=queued.append,
    )

    assert outcome is JobOutcome.DONE
    assert mp4_job.done_with["hls_status"] is HlsStatus.PENDING
    assert queued == [mp4_job.id]


async def test_no_ladder_promised_means_none_queued_and_the_status_is_derived(
    mp4_job, session_factory
):
    queued: list[UUID] = []

    await process_job_async(
        mp4_job.id,
        session_factory=session_factory,
        step=_Step(ProcessingResult(output_key="outputs/a/demo.mp4")),
        enqueue_ladder_fn=queued.append,
    )

    assert mp4_job.done_with["hls_status"] is None
    assert queued == []


async def test_a_ladder_that_cannot_be_queued_is_settled_unavailable_at_once(
    mp4_job, session_factory, monkeypatch
):
    """The video is already done; leaving the ladder pending would show
    "HD processing" for half an hour until the reaper noticed.
    """
    given_up: list[UUID] = []

    async def mark_ladder_unavailable(_session, job_id):
        given_up.append(job_id)

    def redis_down(_job_id):
        raise ConnectionError("redis unreachable")

    monkeypatch.setattr(tasks, "mark_ladder_unavailable", mark_ladder_unavailable)

    outcome = await process_job_async(
        mp4_job.id,
        session_factory=session_factory,
        step=_Step(ProcessingResult(output_key="outputs/a/demo.mp4", ladder_pending=True)),
        enqueue_ladder_fn=redis_down,
    )

    assert outcome is JobOutcome.DONE
    assert given_up == [mp4_job.id]


def test_the_rq_entrypoint_wires_the_real_dependencies(monkeypatch):
    seen = {}

    async def fake_build(job_id, *, session_factory, ladder_step):
        seen.update(job_id=job_id, session_factory=session_factory, ladder_step=ladder_step)
        return HlsStatus.READY

    monkeypatch.setattr(tasks, "build_ladder_async", fake_build)
    monkeypatch.setattr(tasks, "get_worker_session_factory", lambda: "sessions")
    monkeypatch.setattr(tasks, "get_ladder_step", lambda: "ladder")
    job_id = uuid4()

    assert tasks.build_ladder(str(job_id)) == "ready"
    assert seen == {"job_id": job_id, "session_factory": "sessions", "ladder_step": "ladder"}


def test_the_rq_entrypoint_reports_a_skipped_entry(monkeypatch):
    async def fake_build(job_id, **_kwargs):
        return None

    monkeypatch.setattr(tasks, "build_ladder_async", fake_build)
    monkeypatch.setattr(tasks, "get_worker_session_factory", lambda: None)
    monkeypatch.setattr(tasks, "get_ladder_step", lambda: None)

    assert tasks.build_ladder(str(uuid4())) == "skipped"


# --- the queue -------------------------------------------------------------------


def test_a_ladder_is_queued_under_an_id_traceable_to_its_job():
    queue = Queue("video_jobs-ladder", connection=fakeredis.FakeStrictRedis())
    job_id = uuid4()

    rq_id = enqueue_ladder(job_id, queue=queue)

    assert rq_id == ladder_rq_job_id(job_id) == f"{job_id}-ladder"
    rq_job = queue.fetch_job(rq_id)
    assert rq_job.func_name == BUILD_LADDER_TASK
    assert rq_job.func is tasks.build_ladder, "worker cannot import what it enqueued"


# --- the builder fetches its own input -------------------------------------------


class _Store:
    def __init__(self) -> None:
        self.downloads: list[str] = []
        self.deleted: list[str] = []

    def download_file(self, *, key, destination):
        self.downloads.append(key)
        Path(destination).write_bytes(b"src")

    def delete_prefix(self, prefix):
        self.deleted.append(prefix)


def test_the_builder_downloads_its_input_and_cleans_its_temp_dir(monkeypatch):
    store = _Store()
    builder = HlsLadderBuilder(store, output_prefix="outputs")
    seen: dict = {}

    def fake_build(*, job_id, source_path, probe):
        seen["path"] = source_path
        assert source_path.read_bytes() == b"src"
        return "outputs/x/hls/master.m3u8"

    monkeypatch.setattr(builder, "build", fake_build)

    key = builder.run(job_id=uuid4(), source_key="uploads/a/demo.mov", probe=object())

    assert key == "outputs/x/hls/master.m3u8"
    assert store.downloads == ["uploads/a/demo.mov"]
    assert not seen["path"].parent.exists()


def test_discarding_a_ladder_removes_only_that_jobs_ladder_prefix():
    store = _Store()
    job_id = uuid4()

    HlsLadderBuilder(store, output_prefix="outputs").discard(job_id)

    assert store.deleted == [f"outputs/{job_id}/hls/"]


# --- the reaper --------------------------------------------------------------------


@pytest.fixture
def reaper_env(monkeypatch):
    """A reaper pass with nothing stale except the ladders under test."""
    state = SimpleNamespace(ladders=[], queue_status={}, requeued=[], given_up=[])

    @asynccontextmanager
    async def sessions():
        yield object()

    async def nothing(*_args, **_kwargs):
        return []

    async def list_stale_ladders(_session, *, before):
        assert before < datetime.now(UTC) - timedelta(seconds=1700)
        return state.ladders

    async def mark_ladder_unavailable(_session, job_id):
        state.given_up.append(job_id)
        return object()

    async def no_keys(_session):
        return set()

    monkeypatch.setattr(reaper, "get_settings", lambda: SimpleNamespace(
        reaper_lease_seconds=1800, reaper_orphan_grace_seconds=3600
    ))
    monkeypatch.setattr(reaper, "get_session_factory", lambda: sessions)
    monkeypatch.setattr(reaper, "list_stale", nothing)
    monkeypatch.setattr(reaper, "list_source_keys", no_keys)
    monkeypatch.setattr(reaper, "list_stale_ladders", list_stale_ladders)
    monkeypatch.setattr(reaper, "mark_ladder_unavailable", mark_ladder_unavailable)
    storage = SimpleNamespace(list_objects=nothing)
    monkeypatch.setattr(reaper, "get_storage_service", lambda: storage)
    monkeypatch.setattr(reaper, "get_redis_connection", lambda: object())
    monkeypatch.setattr(reaper, "enqueue_ladder", state.requeued.append)
    monkeypatch.setattr(
        reaper, "_ladder_queue_status", lambda job_id, _conn: state.queue_status.get(job_id)
    )
    return state


async def test_a_ladder_still_waiting_in_a_long_queue_is_left_alone(reaper_env):
    job = SimpleNamespace(id=uuid4())
    reaper_env.ladders = [job]
    reaper_env.queue_status = {job.id: reaper.RqStatus.QUEUED}

    assert await reaper.run_once() == (0, 0, 0)


async def test_a_ladder_lost_from_redis_is_queued_again(reaper_env):
    job = SimpleNamespace(id=uuid4())
    reaper_env.ladders = [job]

    assert await reaper.run_once() == (0, 1, 0)
    assert reaper_env.requeued == [job.id]


async def test_a_ladder_whose_worker_died_is_given_up_on(reaper_env):
    """RQ moves a job whose worker vanished to failed once its timeout passes;
    without this the page would say "HD processing" forever.
    """
    job = SimpleNamespace(id=uuid4())
    reaper_env.ladders = [job]
    reaper_env.queue_status = {job.id: reaper.RqStatus.FAILED}

    assert await reaper.run_once() == (1, 0, 0)
    assert reaper_env.given_up == [job.id]


def test_the_reaper_reads_a_ladders_queue_state_by_its_traceable_id():
    connection = fakeredis.FakeStrictRedis()
    job_id = uuid4()
    enqueue_ladder(job_id, queue=Queue("video_jobs-ladder", connection=connection))

    assert reaper._ladder_queue_status(job_id, connection) is reaper.RqStatus.QUEUED
    assert reaper._ladder_queue_status(uuid4(), connection) is None
