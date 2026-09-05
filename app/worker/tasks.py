"""The job task and its state machine.

Contract rules this file exists to enforce:

* The worker is the sole writer to `status`, `output_key`, `error` and
  `updated_at` after the API's insert (N4). Every write here goes through
  `app.repositories.jobs`, never raw SQL.
* Transitions are one-way: queued -> processing -> done | failed. There are no
  retries in sprint 1.
* A job that fails reaches `failed` with a readable error. It never hangs in
  `processing` because of an exception this process could see (N3).
* Every transition is logged with the job id (N9).
"""

import asyncio
import logging
from enum import StrEnum
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.repositories.jobs import (
    InvalidJobTransitionError,
    JobNotFoundError,
    mark_done,
    mark_failed,
    mark_processing,
)
from app.worker.db import get_worker_session_factory
from app.worker.storage import ObjectStoreError, ProcessingStep, get_processing_step

logger = logging.getLogger("app.worker")

# Postgres would take a much longer string, but an error column is read by a
# human in a UI, not parsed. Keep it to something that fits on a screen.
MAX_ERROR_LENGTH = 500


class JobOutcome(StrEnum):
    DONE = "done"
    FAILED = "failed"
    SKIPPED = "skipped"


def readable_error(exception: BaseException) -> str:
    """Turn an exception into something worth showing a user.

    `ObjectStoreError` messages are written to be read, so they pass through
    as-is. Anything else is unexpected, so the class name is kept for
    diagnosis. Never returns an empty string: `mark_failed` rejects those, and
    a blank error column is exactly the "left guessing" case US4 is about.
    """
    message = str(exception).strip()
    if not message:
        message = exception.__class__.__name__
    elif not isinstance(exception, ObjectStoreError):
        message = f"{exception.__class__.__name__}: {message}"

    if len(message) > MAX_ERROR_LENGTH:
        message = message[: MAX_ERROR_LENGTH - 3].rstrip() + "..."
    return message


async def process_job_async(
    job_id: UUID,
    *,
    session_factory: async_sessionmaker[AsyncSession],
    step: ProcessingStep,
) -> JobOutcome:
    # 1. Claim the job. The conditional update in `mark_processing` is what
    #    makes this safe with N workers racing on the same queue entry: exactly
    #    one of them moves queued -> processing, the rest get told no.
    async with session_factory() as session:
        try:
            job = await mark_processing(session, job_id)
        except JobNotFoundError:
            logger.warning("job %s: not in database, dropping queue entry", job_id)
            return JobOutcome.SKIPPED
        except InvalidJobTransitionError as exc:
            # Duplicate delivery, or another worker got there first. Not an
            # error: the job is already someone else's, or already finished.
            logger.info("job %s: not claimable, leaving it alone (%s)", job_id, exc)
            return JobOutcome.SKIPPED

    source_key = job.source_key
    logger.info("job %s: queued -> processing (source_key=%s)", job_id, source_key)

    # 2. Do the work with no database connection held. The step is blocking
    #    object-store I/O, so it goes to a thread rather than stalling the loop.
    try:
        output_key = await asyncio.to_thread(step.run, job_id=job_id, source_key=source_key)
    except Exception as exc:
        reason = readable_error(exc)
        logger.exception("job %s: processing raised, marking failed", job_id)
        async with session_factory() as session:
            try:
                await mark_failed(session, job_id, error=reason)
            except (JobNotFoundError, InvalidJobTransitionError) as write_exc:
                logger.error("job %s: could not record failure: %s", job_id, write_exc)
                return JobOutcome.SKIPPED
        logger.info("job %s: processing -> failed (%s)", job_id, reason)
        return JobOutcome.FAILED

    # 3. Record success.
    async with session_factory() as session:
        try:
            await mark_done(session, job_id, output_key=output_key)
        except (JobNotFoundError, InvalidJobTransitionError) as write_exc:
            logger.error("job %s: could not record completion: %s", job_id, write_exc)
            return JobOutcome.SKIPPED

    logger.info("job %s: processing -> done (output_key=%s)", job_id, output_key)
    return JobOutcome.DONE


def process_job(job_id: str) -> str:
    """RQ entrypoint. Enqueued by the API as `app.worker.tasks.process_job`.

    Takes the job id as a string because that is what survives a round trip
    through the queue cleanly.
    """
    parsed = UUID(job_id)
    outcome = asyncio.run(
        process_job_async(
            parsed,
            session_factory=get_worker_session_factory(),
            step=get_processing_step(),
        )
    )
    return outcome.value
