"""Recover jobs and objects left behind by interrupted requests or workers."""

import asyncio
import logging
from datetime import UTC, datetime, timedelta

from rq.exceptions import NoSuchJobError
from rq.job import Job as RqJob
from rq.job import JobStatus as RqStatus
from rq.registry import StartedJobRegistry

from app.config import get_settings
from app.database import get_session_factory
from app.models.job import JobStatus
from app.queue import (
    enqueue_job,
    enqueue_ladder,
    get_ladder_queue,
    get_queue,
    get_redis_connection,
    ladder_rq_job_id,
)
from app.repositories.jobs import (
    list_source_keys,
    list_stale,
    list_stale_ladders,
    mark_ladder_unavailable,
    mark_stale_failed,
)
from app.services.storage import get_storage_service
from app.services.upload_cleanup import cleanup_expired
from app.worker.__main__ import configure_logging

logger = logging.getLogger("app.worker.reaper")

# A ladder whose queue entry is in one of these is still going to run, however
# long the queue is. Anything else -- failed, stopped, or finished without
# writing -- will never settle the row on its own.
_LADDER_STILL_COMING = {
    RqStatus.QUEUED,
    RqStatus.STARTED,
    RqStatus.DEFERRED,
    RqStatus.SCHEDULED,
}


def _heartbeat_alive(rq_job_id: str, queue, connection) -> bool:
    """Whether a worker is still keeping this queue entry alive.

    A running job sits in its queue's started registry with an expiry about
    90 seconds ahead, which the worker pushes forward every 30 seconds
    (RQ's heartbeat). A worker that dies -- killed, out of memory, its
    instance gone without warning -- stops pushing, and the entry lapses.
    Read-only, like the queue-depth metric: RQ's own cleanup of the registry
    is left to RQ.
    """
    key = StartedJobRegistry(queue=queue).key
    live = connection.zrangebyscore(key, datetime.now(UTC).timestamp(), "+inf")
    prefix = f"{rq_job_id}:".encode()
    return any(member.startswith(prefix) for member in live)


def _ladder_queue_status(job_id, connection) -> RqStatus | None:
    try:
        return RqJob.fetch(ladder_rq_job_id(job_id), connection=connection).get_status()
    except NoSuchJobError:
        return None


async def _settle_stale_ladders(session, *, before, connection) -> tuple[int, int]:
    """Re-queue a lost ladder, give up on a dead one. Returns (requeued, settled)."""
    requeued = settled = 0
    ladder_queue = get_ladder_queue(connection)
    for job in await list_stale_ladders(session, before=before):
        status = await asyncio.to_thread(_ladder_queue_status, job.id, connection)
        if status is RqStatus.STARTED and not await asyncio.to_thread(
            _heartbeat_alive, ladder_rq_job_id(job.id), ladder_queue, connection
        ):
            # RQ still says started, but no worker is keeping it alive: the
            # worker died without warning. The MP4 still plays; give up on
            # the ladder rather than loop on a video that may have killed it.
            if await mark_ladder_unavailable(session, job.id) is not None:
                logger.warning("job %s: ladder pending -> unavailable (its worker died)", job.id)
                settled += 1
            continue
        if status in _LADDER_STILL_COMING:
            continue
        if status is None:
            # Never reached Redis, or Redis lost it: same as a stale queued job.
            await asyncio.to_thread(enqueue_ladder, job.id)
            logger.info("job %s: re-enqueued lost HLS ladder", job.id)
            requeued += 1
        elif await mark_ladder_unavailable(session, job.id) is not None:
            logger.warning("job %s: ladder pending -> unavailable (queue says %s)", job.id, status)
            settled += 1
    return requeued, settled


async def run_once() -> tuple[int, int, int]:
    settings = get_settings()
    now = datetime.now(UTC)
    lease_before = now - timedelta(seconds=settings.reaper_lease_seconds)
    abandoned_before = now - timedelta(seconds=settings.reaper_abandoned_seconds)
    grace_before = now - timedelta(seconds=settings.reaper_orphan_grace_seconds)
    reaped = requeued = deleted = 0

    async with get_session_factory()() as session:
        connection = await asyncio.to_thread(get_redis_connection)
        job_queue = get_queue(connection)
        # A job processing for a few minutes is checked against its worker's
        # heartbeat rather than waiting out the whole lease: a dead worker is
        # found in minutes. The lease stays as the backstop for a worker that
        # is alive but stuck.
        for job in await list_stale(session, status=JobStatus.PROCESSING, before=abandoned_before):
            hung = job.updated_at < lease_before
            if not hung and await asyncio.to_thread(
                _heartbeat_alive, str(job.id), job_queue, connection
            ):
                continue
            if await mark_stale_failed(session, job.id) is not None:
                reason = "lease expired" if hung else "its worker died"
                logger.warning("job %s: processing -> failed (%s)", job.id, reason)
                reaped += 1
        queued = await list_stale(session, status=JobStatus.QUEUED, before=grace_before)
        for job in queued:
            exists = await asyncio.to_thread(RqJob.exists, str(job.id), connection=connection)
            if not exists:
                await asyncio.to_thread(enqueue_job, job.id)
                logger.info("job %s: re-enqueued stale queued job", job.id)
                requeued += 1
        ladders_requeued, ladders_settled = await _settle_stale_ladders(
            session, before=abandoned_before, connection=connection
        )
        requeued += ladders_requeued
        reaped += ladders_settled

        source_keys = await list_source_keys(session)

    storage = get_storage_service()
    for key, modified in await storage.list_objects(prefix="uploads/"):
        if key not in source_keys and modified is not None and modified < grace_before:
            await storage.delete_object(key)
            logger.warning("orphan object deleted: %s", key)
            deleted += 1
    return reaped, requeued, deleted


async def main() -> None:
    configure_logging("INFO")
    settings = get_settings()
    while True:
        try:
            reaped, requeued, deleted = await run_once()
            await cleanup_expired()
            logger.info(
                "reaper pass complete: reaped=%s requeued=%s deleted=%s",
                reaped,
                requeued,
                deleted,
            )
        except Exception:
            logger.exception("reaper pass failed; retrying")
        await asyncio.sleep(settings.reaper_interval_seconds)


if __name__ == "__main__":
    asyncio.run(main())
