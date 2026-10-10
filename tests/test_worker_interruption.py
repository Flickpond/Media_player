"""A worker told to stop gives its job back instead of failing it.

Spot reclaims and scale-in stop workers routinely; the uploader must never
see `failed` for a video that was never at fault.
"""

import signal
import subprocess
import sys
import threading
import time
from contextlib import asynccontextmanager
from types import SimpleNamespace
from uuid import uuid4

import pytest
from rq import Retry

from app.models.job import HlsStatus, JobStatus
from app.worker import interruption, tasks
from app.worker.__main__ import GiveBackWorker
from app.worker.tasks import (
    INTERRUPTED_TOO_OFTEN,
    JobOutcome,
    build_ladder_async,
    process_job_async,
)

PYTHON = sys.executable


@pytest.fixture(autouse=True)
def clean_stop_flag():
    interruption.reset()
    yield
    interruption.reset()


# --- the runner ----------------------------------------------------------------


def test_the_runner_behaves_like_subprocess_run():
    result = interruption.run(
        [PYTHON, "-c", "print('ok')"], capture_output=True, text=True, timeout=30, check=False
    )

    assert result.returncode == 0 and result.stdout.strip() == "ok"


def test_the_runner_still_enforces_its_timeout():
    with pytest.raises(subprocess.TimeoutExpired):
        interruption.run([PYTHON, "-c", "import time; time.sleep(30)"], timeout=0.5)


def test_a_stop_ends_a_running_process_in_seconds_not_minutes():
    """This is what turns a 100-second grace period into enough time."""
    outcome = {}

    def encode():
        started = time.monotonic()
        result = interruption.run([PYTHON, "-c", "import time; time.sleep(60)"], timeout=120)
        outcome.update(code=result.returncode, seconds=time.monotonic() - started)

    worker = threading.Thread(target=encode)
    worker.start()
    time.sleep(1.0)
    interruption.request_stop()
    worker.join(timeout=15)

    assert not worker.is_alive()
    assert outcome["code"] != 0 and outcome["seconds"] < 15


def test_no_new_process_starts_once_a_stop_is_requested():
    """A job between two encodes would otherwise start the second one and be
    killed mid-way through it anyway."""
    interruption.request_stop()

    with pytest.raises(interruption.Interrupted):
        interruption.run([PYTHON, "-c", "print('never')"])


def test_sigterm_during_a_job_requests_a_stop_and_chains_to_the_old_handler():
    seen = []
    previous = signal.signal(signal.SIGTERM, lambda signum, frame: seen.append(signum))
    try:
        with interruption.stop_on_sigterm():
            handler = signal.getsignal(signal.SIGTERM)
            handler(signal.SIGTERM, None)  # what the signal would do
            assert interruption.stop_requested()
            assert seen == [signal.SIGTERM], "RQ's warm shutdown must still happen"
        assert not interruption.stop_requested(), "the next job starts clean"
        assert signal.getsignal(signal.SIGTERM) is not handler
    finally:
        signal.signal(signal.SIGTERM, previous)


def test_a_forked_job_does_not_chain_to_the_default_action():
    """In a work horse the old handler is 'terminate' -- the thing to avoid."""
    previous = signal.signal(signal.SIGTERM, signal.SIG_DFL)
    try:
        with interruption.stop_on_sigterm():
            signal.getsignal(signal.SIGTERM)(signal.SIGTERM, None)  # must not exit
            assert interruption.stop_requested()
    finally:
        signal.signal(signal.SIGTERM, previous)


# --- the MP4 job gives itself back --------------------------------------------


@asynccontextmanager
async def _sessions():
    yield object()


@pytest.fixture
def rows(monkeypatch):
    job = SimpleNamespace(id=uuid4(), source_key="uploads/a/clip.mp4", max_fps=30)
    calls = []

    async def mark_processing(_session, _job_id):
        return job

    async def mark_interrupted(_session, job_id):
        calls.append(("interrupted", job_id))
        return job

    async def mark_failed(_session, job_id, *, error):
        calls.append(("failed", error))
        return job

    monkeypatch.setattr(tasks, "mark_processing", mark_processing)
    monkeypatch.setattr(tasks, "mark_interrupted", mark_interrupted)
    monkeypatch.setattr(tasks, "mark_failed", mark_failed)
    return SimpleNamespace(job=job, calls=calls)


class _StoppedStep:
    """A step whose FFmpeg was killed by the stop."""

    def run(self, *, job_id, source_key, max_fps=30):
        interruption.request_stop()
        raise RuntimeError("FFmpeg exited with signal 15")


async def test_a_stopped_job_goes_back_to_the_queue_not_to_failed(rows):
    outcome = await process_job_async(
        rows.job.id, session_factory=lambda: _sessions(), step=_StoppedStep()
    )

    assert outcome is JobOutcome.INTERRUPTED
    assert rows.calls == [("interrupted", rows.job.id)]


async def test_the_third_interruption_fails_with_a_readable_error(rows):
    outcome = await process_job_async(
        rows.job.id, session_factory=lambda: _sessions(), step=_StoppedStep(), interruptions=2
    )

    assert outcome is JobOutcome.FAILED
    assert rows.calls == [("failed", INTERRUPTED_TOO_OFTEN)]


async def test_a_real_failure_with_no_stop_still_fails(rows):
    class Broken:
        def run(self, *, job_id, source_key, max_fps=30):
            raise RuntimeError("corrupt input")

    outcome = await process_job_async(
        rows.job.id, session_factory=lambda: _sessions(), step=Broken()
    )

    assert outcome is JobOutcome.FAILED
    assert rows.calls[0][0] == "failed"


def test_an_interrupted_job_asks_rq_to_run_it_again(monkeypatch):
    async def interrupted(job_id, **_kwargs):
        return JobOutcome.INTERRUPTED

    monkeypatch.setattr(tasks, "process_job_async", interrupted)
    monkeypatch.setattr(tasks, "get_worker_session_factory", lambda: None)
    monkeypatch.setattr(tasks, "get_processing_step", lambda: None)

    result = tasks.process_job(str(uuid4()))

    assert isinstance(result, Retry) and result.max == tasks.MAX_INTERRUPTIONS


def test_previous_interruptions_come_from_rqs_retry_count(monkeypatch):
    monkeypatch.setattr(tasks, "get_current_job", lambda: SimpleNamespace(number_of_retries=2))
    assert tasks._previous_interruptions() == 2

    monkeypatch.setattr(tasks, "get_current_job", lambda: None)
    assert tasks._previous_interruptions() == 0


# --- the ladder job stays pending ----------------------------------------------


@pytest.fixture
def ladder_job(monkeypatch):
    job = SimpleNamespace(
        id=uuid4(),
        status=JobStatus.DONE.value,
        hls_status=HlsStatus.PENDING.value,
        source_key="uploads/a/clip.mp4",
        output_key="outputs/a/clip.mp4",
        operations=None,
        width=1920,
        height=1080,
        duration_seconds=10.0,
        max_fps=30,
    )
    given_up = []

    async def get_job(_session, _job_id):
        return job

    async def mark_ladder_unavailable(_session, job_id):
        given_up.append(job_id)
        return job

    monkeypatch.setattr(tasks, "get_job", get_job)
    monkeypatch.setattr(tasks, "mark_ladder_unavailable", mark_ladder_unavailable)
    return SimpleNamespace(job=job, given_up=given_up)


class _StoppedLadder:
    def __init__(self):
        self.discarded = []

    def run(self, *, job_id, source_key, probe, max_fps=30):
        interruption.request_stop()
        raise RuntimeError("FFmpeg exited with signal 15")

    def discard(self, job_id):
        self.discarded.append(job_id)


async def test_a_stopped_ladder_stays_pending_and_drops_its_partial_upload(ladder_job):
    ladder = _StoppedLadder()

    settled = await build_ladder_async(
        ladder_job.job.id, session_factory=lambda: _sessions(), ladder_step=ladder
    )

    assert settled is JobOutcome.INTERRUPTED
    assert ladder.discarded == [ladder_job.job.id]
    assert ladder_job.given_up == [], "still pending: another worker builds it"


async def test_the_third_ladder_interruption_gives_up_on_the_ladder(ladder_job):
    settled = await build_ladder_async(
        ladder_job.job.id,
        session_factory=lambda: _sessions(),
        ladder_step=_StoppedLadder(),
        interruptions=2,
    )

    assert settled is HlsStatus.UNAVAILABLE
    assert ladder_job.given_up == [ladder_job.job.id]


def test_an_interrupted_ladder_asks_rq_to_run_it_again(monkeypatch):
    async def interrupted(job_id, **_kwargs):
        return JobOutcome.INTERRUPTED

    monkeypatch.setattr(tasks, "build_ladder_async", interrupted)
    monkeypatch.setattr(tasks, "get_worker_session_factory", lambda: None)
    monkeypatch.setattr(tasks, "get_ladder_step", lambda: None)

    assert isinstance(tasks.build_ladder(str(uuid4())), Retry)


# --- the worker passes the stop on ------------------------------------------------


def test_the_worker_forwards_a_stop_to_the_job_it_is_running(monkeypatch):
    sent = []
    monkeypatch.setattr("app.worker.__main__.os.kill", lambda pid, sig: sent.append((pid, sig)))
    worker = GiveBackWorker.__new__(GiveBackWorker)
    worker._horse_pid = 4242
    worker.log = SimpleNamespace(info=lambda *a, **k: None)
    worker.name, worker.pid = "w", 1

    worker.handle_warm_shutdown_request()

    assert sent == [(4242, signal.SIGTERM)]


def test_an_idle_worker_has_no_job_to_forward_to(monkeypatch):
    sent = []
    monkeypatch.setattr("app.worker.__main__.os.kill", lambda pid, sig: sent.append((pid, sig)))
    worker = GiveBackWorker.__new__(GiveBackWorker)
    worker._horse_pid = 0
    worker.log = SimpleNamespace(info=lambda *a, **k: None)
    worker.name, worker.pid = "w", 1

    worker.handle_warm_shutdown_request()

    assert sent == []


# --- the repository's one way back -------------------------------------------------


async def test_giving_back_is_conditional_on_the_job_still_processing():
    from sqlalchemy.dialects import postgresql

    from app.repositories.jobs import mark_interrupted

    statements = []

    class Session:
        async def execute(self, statement):
            compiled = statement.compile(dialect=postgresql.dialect())
            statements.append((str(compiled), compiled.params))
            return SimpleNamespace(scalar_one_or_none=lambda: "job")

        async def commit(self):
            pass

    assert await mark_interrupted(Session(), uuid4()) == "job"
    sql, params = statements[0]
    assert "jobs.status = " in sql
    assert "processing" in params.values() and "queued" in params.values()
    # Cleared for the next run, like any other transition.
    assert params["output_key"] is None and params["hls_status"] == "pending"
