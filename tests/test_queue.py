"""The enqueue -> consume seam.

This is the boundary between B's API and A's worker. It runs a real RQ queue
and a real RQ worker against an in-memory Redis, so a mismatch between the
enqueued task path and what the worker can resolve fails here rather than
silently on integration day.
"""

from uuid import uuid4

import fakeredis
import pytest
from rq import Queue, SimpleWorker

from app.queue import PROCESS_JOB_TASK, enqueue_job
from app.worker import tasks


@pytest.fixture
def connection():
    return fakeredis.FakeStrictRedis()


@pytest.fixture
def queue(connection):
    return Queue("video_jobs", connection=connection)


@pytest.fixture
def executed(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """Replace the task body; we are testing the plumbing, not the state machine."""
    calls: list[str] = []

    def fake_process_job(job_id: str) -> str:
        calls.append(job_id)
        return "done"

    monkeypatch.setattr(tasks, "process_job", fake_process_job)
    return calls


def run_worker(queue, connection, *, max_jobs=None) -> None:
    worker = SimpleWorker([queue], connection=connection)
    worker.work(burst=True, max_jobs=max_jobs)


def test_enqueued_job_is_picked_up_and_run_by_a_worker(queue, connection, executed):
    job_id = uuid4()

    enqueue_job(job_id, queue=queue)
    assert queue.count == 1

    run_worker(queue, connection)

    assert executed == [str(job_id)]
    assert queue.count == 0


def test_queue_entry_is_addressable_by_the_database_job_id(queue, executed):
    job_id = uuid4()

    rq_job_id = enqueue_job(job_id, queue=queue)

    assert rq_job_id == str(job_id)
    assert queue.fetch_job(str(job_id)) is not None


def test_worker_resolves_the_task_path_the_api_enqueues(queue, executed):
    """A typo in PROCESS_JOB_TASK is silent in production -- catch it here."""
    job_id = uuid4()
    enqueue_job(job_id, queue=queue)

    rq_job = queue.fetch_job(str(job_id))

    assert rq_job.func_name == PROCESS_JOB_TASK
    assert rq_job.func is tasks.process_job, "worker cannot import what the API enqueued"


def test_two_workers_share_one_queue_with_no_job_affinity(queue, connection, executed):
    """N2/N5: any worker takes any job, each job runs exactly once."""
    job_ids = [uuid4() for _ in range(4)]
    for job_id in job_ids:
        enqueue_job(job_id, queue=queue)

    run_worker(queue, connection, max_jobs=2)
    first_worker_took = list(executed)

    run_worker(queue, connection)
    second_worker_took = executed[len(first_worker_took) :]

    assert len(first_worker_took) == 2
    assert len(second_worker_took) == 2
    assert sorted(executed) == sorted(str(job_id) for job_id in job_ids)
    assert queue.count == 0


def test_failed_task_does_not_silently_vanish_from_the_queue(queue, connection, monkeypatch):
    """If the task process itself dies, RQ must record it, not drop it (N3)."""

    def exploding_process_job(job_id: str) -> str:
        raise RuntimeError("worker process fault")

    monkeypatch.setattr(tasks, "process_job", exploding_process_job)
    job_id = uuid4()
    enqueue_job(job_id, queue=queue)

    run_worker(queue, connection)

    rq_job = queue.failed_job_registry.get_job_ids()
    assert rq_job == [str(job_id)]
