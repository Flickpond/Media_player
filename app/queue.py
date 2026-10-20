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
from app.services.worker_wake import wake_workers

# Enqueue by string reference rather than by importing the callable: the API
# process should not have to import the worker's MinIO dependencies just to
# put a job id on the queue.
PROCESS_JOB_TASK = "app.worker.tasks.process_job"
BUILD_LADDER_TASK = "app.worker.tasks.build_ladder"
HELP_ENCODE_TASK = "app.worker.tasks.help_encode"
HELP_LADDER_TASK = "app.worker.tasks.help_ladder"
BUILD_RENDITION_TASK = "app.worker.tasks.build_rendition"

# Ladders get a queue of their own, named after the main one, so a worker can
# list the two in priority order: an uploader waiting for their MP4 is never
# stuck behind someone else's 4K ladder. Derived rather than configured, so
# the API, every worker and the reaper cannot disagree about it.
LADDER_QUEUE_SUFFIX = "-ladder"
# Pieces of a chunked MP4 (app.worker.chunked): after uploads waiting for a
# worker of their own, before ladders, since a piece speeds up a video an
# uploader is already waiting on.
CHUNK_QUEUE_SUFFIX = "-chunks"


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


def get_ladder_queue(connection: Redis | None = None) -> Queue:
    settings = get_settings()
    return Queue(
        settings.redis_queue + LADDER_QUEUE_SUFFIX,
        connection=connection or get_redis_connection(),
        default_timeout=settings.worker_ladder_timeout_seconds,
    )


def get_chunk_queue(connection: Redis | None = None) -> Queue:
    settings = get_settings()
    return Queue(
        settings.redis_queue + CHUNK_QUEUE_SUFFIX,
        connection=connection or get_redis_connection(),
        default_timeout=settings.worker_job_timeout_seconds,
    )


def enqueue_helper(job_id: UUID, *, queue: Queue | None = None) -> str:
    """Ask one more worker to help encode `job_id`'s pieces."""
    target = queue or get_chunk_queue()
    return target.enqueue(HELP_ENCODE_TASK, str(job_id)).id


def enqueue_ladder_helper(job_id: UUID, *, queue: Queue | None = None) -> str:
    """Ask one more worker to help encode `job_id`'s ladder pieces. On the
    pieces queue, so it comes before ladders; given the ladder's own limit,
    since a helper may take several 4K pieces."""
    target = queue or get_chunk_queue()
    timeout = get_settings().worker_ladder_timeout_seconds
    return target.enqueue(HELP_LADDER_TASK, str(job_id), job_timeout=timeout).id


def enqueue_rendition(job_id: UUID | str, kind: str, *, queue: Queue | None = None) -> str:
    """Queue an extra AV1 rendition (`hdr` or `av1`) for a job whose ladder
    is ready. On the ladder queue: it is optional, so it waits behind MP4s."""
    target = queue or get_ladder_queue()
    return target.enqueue(
        BUILD_RENDITION_TASK, str(job_id), kind, job_id=f"{job_id}-{kind}"
    ).id


def ladder_rq_job_id(job_id: UUID) -> str:
    """The ladder's queue entry is traceable to its row like the job's is,
    with a suffix because RQ ids are unique across queues."""
    return f"{job_id}{LADDER_QUEUE_SUFFIX}"


def enqueue_job(job_id: UUID, *, queue: Queue | None = None) -> str:
    """Put a job id on the queue. Called by the API after the row is inserted.

    Returns the RQ job id, which is set to the database job id so a queue entry
    can be traced back to its row without a lookup table.
    """
    target = queue or get_queue()
    enqueued = target.enqueue(PROCESS_JOB_TASK, str(job_id), job_id=str(job_id))
    # Overnight the pool is at zero: start a worker now rather than after
    # the scale-out alarm's two minutes of samples. Best-effort, never raises.
    wake_workers(target.connection)
    return enqueued.id


def enqueue_ladder(job_id: UUID, *, queue: Queue | None = None) -> str:
    """Queue the adaptive ladder for a job the worker has just marked done."""
    target = queue or get_ladder_queue()
    enqueued = target.enqueue(BUILD_LADDER_TASK, str(job_id), job_id=ladder_rq_job_id(job_id))
    return enqueued.id
