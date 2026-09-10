"""Recover jobs and objects left behind by interrupted requests or workers."""

import asyncio
import logging
from datetime import UTC, datetime, timedelta

from rq.job import Job as RqJob

from app.config import get_settings
from app.database import get_session_factory
from app.models.job import JobStatus
from app.queue import enqueue_job, get_redis_connection
from app.repositories.jobs import list_source_keys, list_stale, mark_stale_failed
from app.services.storage import get_storage_service

logger = logging.getLogger("app.worker.reaper")


async def run_once() -> tuple[int, int, int]:
    settings = get_settings()
    now = datetime.now(UTC)
    lease_before = now - timedelta(seconds=settings.worker_job_timeout_seconds * 2)
    grace_before = now - timedelta(seconds=settings.reaper_orphan_grace_seconds)
    reaped = requeued = deleted = 0

    async with get_session_factory()() as session:
        for job in await list_stale(session, status=JobStatus.PROCESSING, before=lease_before):
            if await mark_stale_failed(session, job.id) is not None:
                logger.warning("job %s: processing -> failed (lease expired)", job.id)
                reaped += 1
        queued = await list_stale(session, status=JobStatus.QUEUED, before=grace_before)
        connection = await asyncio.to_thread(get_redis_connection)
        for job in queued:
            exists = await asyncio.to_thread(RqJob.exists, str(job.id), connection=connection)
            if not exists:
                await asyncio.to_thread(enqueue_job, job.id)
                logger.info("job %s: re-enqueued stale queued job", job.id)
                requeued += 1

        source_keys = await list_source_keys(session)

    storage = get_storage_service()
    for key, modified in await storage.list_objects(prefix="uploads/"):
        if key not in source_keys and modified is not None and modified < grace_before:
            await storage.delete_object(key)
            logger.warning("orphan object deleted: %s", key)
            deleted += 1
    return reaped, requeued, deleted


async def main() -> None:
    settings = get_settings()
    while True:
        try:
            reaped, requeued, deleted = await run_once()
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
