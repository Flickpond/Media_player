import logging
from contextlib import asynccontextmanager
from uuid import UUID, uuid4

import pytest

from app.models.job import JobStatus
from app.repositories.jobs import InvalidJobTransitionError, JobNotFoundError
from app.worker import tasks
from app.worker.storage import UNPROCESSABLE_VIDEO, ObjectStoreError
from app.worker.tasks import JobOutcome, process_job_async, readable_error


class FakeJob:
    def __init__(self, job_id: UUID, source_key: str, status: str) -> None:
        self.id = job_id
        self.source_key = source_key
        self.status = status
        self.output_key: str | None = None
        self.error: str | None = None
        self.filename = "demo.mp4"


class FakeJobStore:
    """Mirrors the conditional-transition semantics of app.repositories.jobs.

    The real thing is exercised against PostgreSQL in
    tests/integration/test_worker_postgres.py; this keeps the state-machine
    tests fast and dependency-free.
    """

    def __init__(self) -> None:
        self.jobs: dict[UUID, FakeJob] = {}
        self.transitions: list[tuple[UUID, str]] = []

    def add(self, *, status: JobStatus = JobStatus.QUEUED, source_key: str = "uploads/demo.mp4"):
        job = FakeJob(uuid4(), source_key, status.value)
        self.jobs[job.id] = job
        return job

    def _require(self, job_id: UUID) -> FakeJob:
        job = self.jobs.get(job_id)
        if job is None:
            raise JobNotFoundError(str(job_id))
        return job

    def _transition(self, job_id: UUID, expected: JobStatus, nxt: JobStatus) -> FakeJob:
        job = self._require(job_id)
        if job.status != expected.value:
            raise InvalidJobTransitionError(
                f"cannot transition job {job_id} from {job.status} to {nxt.value}"
            )
        job.status = nxt.value
        self.transitions.append((job_id, nxt.value))
        return job

    async def mark_processing(self, _session, job_id: UUID) -> FakeJob:
        return self._transition(job_id, JobStatus.QUEUED, JobStatus.PROCESSING)

    async def mark_done(self, _session, job_id: UUID, *, output_key: str) -> FakeJob:
        if not output_key.strip():
            raise ValueError("output_key must not be empty")
        job = self._transition(job_id, JobStatus.PROCESSING, JobStatus.DONE)
        job.output_key = output_key
        return job

    async def mark_failed(self, _session, job_id: UUID, *, error: str) -> FakeJob:
        if not error.strip():
            raise ValueError("failed jobs require a readable error")
        job = self._transition(job_id, JobStatus.PROCESSING, JobStatus.FAILED)
        job.error = error.strip()
        return job


class FakeStep:
    def __init__(self, *, output_key: str = "outputs/demo.mp4", raises: Exception | None = None):
        self.output_key = output_key
        self.raises = raises
        self.calls: list[tuple[UUID, str]] = []

    def run(self, *, job_id: UUID, source_key: str) -> str:
        self.calls.append((job_id, source_key))
        if self.raises is not None:
            raise self.raises
        return self.output_key


@pytest.fixture
def store(monkeypatch: pytest.MonkeyPatch) -> FakeJobStore:
    fake = FakeJobStore()
    monkeypatch.setattr(tasks, "mark_processing", fake.mark_processing)
    monkeypatch.setattr(tasks, "mark_done", fake.mark_done)
    monkeypatch.setattr(tasks, "mark_failed", fake.mark_failed)
    return fake


@pytest.fixture
def session_factory():
    @asynccontextmanager
    async def _factory():
        yield object()

    return _factory


async def test_happy_path_walks_queued_processing_done(store, session_factory):
    job = store.add(source_key="uploads/abc/demo.mp4")
    step = FakeStep(output_key="outputs/abc/demo.mp4")

    outcome = await process_job_async(job.id, session_factory=session_factory, step=step)

    assert outcome is JobOutcome.DONE
    assert job.status == JobStatus.DONE.value
    assert job.output_key == "outputs/abc/demo.mp4"
    assert job.error is None
    assert store.transitions == [(job.id, "processing"), (job.id, "done")]
    assert step.calls == [(job.id, "uploads/abc/demo.mp4")]


async def test_processing_failure_records_the_user_half_not_the_diagnostic(store, session_factory):
    """P9: `GET /jobs/{id}` returns this column verbatim, so it gets their half."""
    job = store.add()
    step = FakeStep(
        raises=ObjectStoreError(
            "source object missing from storage: uploads/x.mp4",
            user_message="the uploaded file is no longer in storage; please upload it again",
        )
    )

    outcome = await process_job_async(job.id, session_factory=session_factory, step=step)

    assert outcome is JobOutcome.FAILED
    assert job.status == JobStatus.FAILED.value
    assert job.error == "the uploaded file is no longer in storage; please upload it again"
    assert "uploads/x.mp4" not in job.error, "an object key must not reach a user-facing column"
    assert job.output_key is None
    assert store.transitions == [(job.id, "processing"), (job.id, "failed")]


async def test_unexpected_exception_still_reaches_failed_never_hangs(store, session_factory):
    """N3: no silent hang in `processing`, whatever the step throws."""
    job = store.add()
    step = FakeStep(raises=RuntimeError("connection reset by peer"))

    outcome = await process_job_async(job.id, session_factory=session_factory, step=step)

    assert outcome is JobOutcome.FAILED
    assert job.status == JobStatus.FAILED.value
    # The row still lands in `failed`, which is what N3 is about. What changed
    # is that it no longer lands there carrying "RuntimeError: ..." (P9).
    assert job.error == tasks.UNEXPECTED_FAILURE
    assert "RuntimeError" not in job.error
    assert "connection reset by peer" not in job.error


async def test_exception_with_blank_message_still_records_something(store, session_factory):
    """An empty error column is US4's "left guessing", and `mark_failed` rejects it."""
    job = store.add()
    step = FakeStep(raises=TimeoutError(""))

    outcome = await process_job_async(job.id, session_factory=session_factory, step=step)

    assert outcome is JobOutcome.FAILED
    assert job.error == tasks.UNEXPECTED_FAILURE
    assert job.error.strip()


async def test_duplicate_delivery_of_a_done_job_is_left_alone(store, session_factory):
    """The design-note case: same job id delivered twice."""
    job = store.add(status=JobStatus.DONE)
    step = FakeStep()

    outcome = await process_job_async(job.id, session_factory=session_factory, step=step)

    assert outcome is JobOutcome.SKIPPED
    assert job.status == JobStatus.DONE.value
    assert store.transitions == []
    assert step.calls == [], "a finished job must not be reprocessed"


async def test_job_already_claimed_by_another_worker_is_left_alone(store, session_factory):
    job = store.add(status=JobStatus.PROCESSING)
    step = FakeStep()

    outcome = await process_job_async(job.id, session_factory=session_factory, step=step)

    assert outcome is JobOutcome.SKIPPED
    assert step.calls == []


async def test_unknown_job_id_is_dropped_not_crashed(store, session_factory):
    step = FakeStep()

    outcome = await process_job_async(uuid4(), session_factory=session_factory, step=step)

    assert outcome is JobOutcome.SKIPPED
    assert step.calls == []


async def test_only_two_workers_can_claim_the_same_job_once(store, session_factory):
    """N5/N2: two replicas racing on one queue entry -- exactly one processes it."""
    job = store.add()
    first = FakeStep(output_key="outputs/first.mp4")
    second = FakeStep(output_key="outputs/second.mp4")

    first_outcome = await process_job_async(job.id, session_factory=session_factory, step=first)
    second_outcome = await process_job_async(job.id, session_factory=session_factory, step=second)

    assert first_outcome is JobOutcome.DONE
    assert second_outcome is JobOutcome.SKIPPED
    assert job.output_key == "outputs/first.mp4"
    assert second.calls == []


async def test_every_transition_is_logged_with_the_job_id(
    store, session_factory, caplog: pytest.LogCaptureFixture
):
    """N9."""
    job = store.add()
    step = FakeStep()

    with caplog.at_level(logging.INFO, logger="app.worker"):
        await process_job_async(job.id, session_factory=session_factory, step=step)

    messages = [record.getMessage() for record in caplog.records]
    assert any("queued -> processing" in m and str(job.id) in m for m in messages)
    assert any("processing -> done" in m and str(job.id) in m for m in messages)


def test_readable_error_truncates_runaway_messages():
    message = readable_error(ObjectStoreError("diagnostic", user_message="x" * 5000))

    assert len(message) <= tasks.MAX_ERROR_LENGTH
    assert message.endswith("...")


# --- what reaches the user, and what must not (P9) -------------------------


def test_an_unexpected_exception_never_names_its_class_or_its_message():
    """This used to render as `KeyError: 'minio_secret_key'` on the user's screen."""
    message = readable_error(KeyError("minio_secret_key"))

    assert message == tasks.UNEXPECTED_FAILURE
    assert "KeyError" not in message
    assert "minio_secret_key" not in message


def test_ffmpeg_stderr_stays_out_of_the_error_column():
    """The worst of the leaks: stderr names the temp path it was working on."""
    stderr = "/tmp/flickpond-transcode-9k2/source: Invalid data found when processing input"
    message = readable_error(
        ObjectStoreError(f"FFmpeg failed: {stderr}", user_message=UNPROCESSABLE_VIDEO)
    )

    assert message == UNPROCESSABLE_VIDEO
    assert "/tmp/" not in message


def test_splitting_the_audiences_does_not_throw_the_operators_half_away():
    """The diagnostic still has to be there, or this trades one gap for another."""
    exc = ObjectStoreError(
        "download failed for uploads/a/b.mp4: AccessDenied",
        user_message="the uploaded file could not be read back; please try again",
    )

    assert "AccessDenied" in str(exc)
    assert "uploads/a/b.mp4" in str(exc)


def test_a_blank_user_message_falls_back_instead_of_emptying_the_column():
    """`mark_failed` rejects empty, and a blank column is US4's "left guessing"."""
    blank = ObjectStoreError("diagnostic", user_message="   ")

    assert readable_error(blank) == tasks.UNEXPECTED_FAILURE


# --- the row moving underneath us -----------------------------------------
#
# These should not happen while the write-ownership rule holds. They are
# covered because the failure mode if it ever breaks is a job stuck in
# `processing` with nobody logging why.


async def test_losing_the_row_before_recording_failure_is_logged_not_raised(
    store, session_factory, monkeypatch, caplog: pytest.LogCaptureFixture
):
    job = store.add()

    async def vanished(_session, job_id, *, error):
        raise JobNotFoundError(str(job_id))

    monkeypatch.setattr(tasks, "mark_failed", vanished)

    with caplog.at_level(logging.ERROR, logger="app.worker"):
        outcome = await process_job_async(
            job.id,
            session_factory=session_factory,
            step=FakeStep(
                raises=ObjectStoreError("storage down", user_message="please try again")
            ),
        )

    assert outcome is JobOutcome.SKIPPED
    assert any("could not record failure" in r.getMessage() for r in caplog.records)


async def test_losing_the_row_before_recording_completion_is_logged_not_raised(
    store, session_factory, monkeypatch, caplog: pytest.LogCaptureFixture
):
    job = store.add()

    async def stolen(_session, job_id, *, output_key):
        raise InvalidJobTransitionError(f"job {job_id} is failed, not processing")

    monkeypatch.setattr(tasks, "mark_done", stolen)

    with caplog.at_level(logging.ERROR, logger="app.worker"):
        outcome = await process_job_async(job.id, session_factory=session_factory, step=FakeStep())

    assert outcome is JobOutcome.SKIPPED
    assert any("could not record completion" in r.getMessage() for r in caplog.records)
