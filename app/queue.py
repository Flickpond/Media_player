"""Shared Redis queue seam.

The API side (B) enqueues job ids here; the worker side (A) consumes them. Both
import this module so the queue name and the task path can never drift apart --
a mismatch there is silent, the job just never runs.
"""

from functools import lru_cache
from uuid import UUID

from redis import Redis
from rq import Queue

from app.config import get_settings

# Enqueue by string reference rather than by importing the callable: the API
# process should not have to import the worker's MinIO dependencies just to
# put a job id on the queue.
PROCESS_JOB_TASK = "app.worker.tasks.process_job"


@lru_cache
def get_redis_connection() -> Redis:
    return Redis.from_url(get_settings().redis_url)


def get_queue(connection: Redis | None = None) -> Queue:
    settings = get_settings()
    return Queue(
        settings.redis_queue,
        connection=connection or get_redis_connection(),
        default_timeout=settings.worker_job_timeout_seconds,
    )


def enqueue_job(job_id: UUID, *, queue: Queue | None = None) -> str:
    """Put a job id on the queue. Called by the API after the row is inserted.

    Returns the RQ job id, which is set to the database job id so a queue entry
    can be traced back to its row without a lookup table.
    """
    target = queue or get_queue()
    enqueued = target.enqueue(PROCESS_JOB_TASK, str(job_id), job_id=str(job_id))
    return enqueued.id
