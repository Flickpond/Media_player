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
import contextlib
import logging
from enum import StrEnum
from uuid import UUID

from rq import Retry, get_current_job
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.models.job import HlsStatus, JobStatus
from app.queue import enqueue_ladder
from app.repositories.jobs import (
    InvalidJobTransitionError,
    JobNotFoundError,
    get_job,
    mark_done,
    mark_failed,
    mark_interrupted,
    mark_ladder_ready,
    mark_ladder_unavailable,
    mark_processing,
)
from app.worker import after_ladder, preview  # noqa: F401  (preview registers its hook)
from app.worker.db import get_worker_session_factory
from app.worker.interruption import stop_on_sigterm, stop_requested
from app.worker.probe import SourceProbe
from app.worker.storage import (
    DEFAULT_MAX_FPS,
    ObjectStoreError,
    ProcessingStep,
    get_edit_processing_step,
    get_ladder_step,
    get_processing_step,
)

logger = logging.getLogger("app.worker")

# Postgres would take a much longer string, but an error column is read by a
# human in a UI, not parsed. Keep it to something that fits on a screen.
MAX_ERROR_LENGTH = 500

# What the uploader sees when the worker hit something nobody anticipated. An
# exception repr is rarely actionable and sometimes names a path, a host or a
# credential, so it stays in the log where the traceback already is.
UNEXPECTED_FAILURE = "processing failed unexpectedly; please try uploading again"


class JobOutcome(StrEnum):
    DONE = "done"
    FAILED = "failed"
    SKIPPED = "skipped"
    # The worker was told to stop mid-job and gave the job back to the queue.
    INTERRUPTED = "interrupted"


# A job interrupted this many times fails instead of going back again: a
# video that reliably takes its worker down with it must not loop forever.
MAX_INTERRUPTIONS = 3
INTERRUPTED_TOO_OFTEN = "processing was interrupted repeatedly; please try uploading again"


def readable_error(exception: BaseException) -> str:
    """The half of a failure that is safe to show the person who uploaded.

    `ObjectStoreError` carries a curated `user_message` for exactly this. Every
    other exception is by definition one nobody wrote a message for, so it gets
    a fixed one.

    This used to splice in `exception.__class__.__name__` and `str(exception)`,
    which is how object keys, temp paths and FFmpeg stderr reached the `error`
    column that `GET /jobs/{id}` returns verbatim (P9). The diagnostic is not
    lost -- it is logged by the caller, which is the audience it was written
    for.

    Never returns an empty string: `mark_failed` rejects those, and a blank
    error column is exactly the "left guessing" case US4 is about.
    """
    if isinstance(exception, ObjectStoreError):
        message = exception.user_message.strip() or UNEXPECTED_FAILURE
    else:
        message = UNEXPECTED_FAILURE

    if len(message) > MAX_ERROR_LENGTH:
        message = message[: MAX_ERROR_LENGTH - 3].rstrip() + "..."
    return message


async def process_job_async(
    job_id: UUID,
    *,
    session_factory: async_sessionmaker[AsyncSession],
    step: ProcessingStep,
    edit_step_factory=get_edit_processing_step,
    enqueue_ladder_fn=enqueue_ladder,
    interruptions: int = 0,
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
    operations = getattr(job, "operations", None)
    selected_step = edit_step_factory(operations) if operations is not None else step
    logger.info("job %s: queued -> processing (source_key=%s)", job_id, source_key)

    # 2. Do the work with no database connection held. The step is blocking
    #    object-store I/O, so it goes to a thread rather than stalling the loop.
    try:
        result = await asyncio.to_thread(
            selected_step.run,
            job_id=job_id,
            source_key=source_key,
            max_fps=getattr(job, "max_fps", DEFAULT_MAX_FPS),
        )
    except Exception as exc:
        if stop_requested():
            # Not the video's fault: the worker is being stopped, and killing
            # FFmpeg is what made the step fail.
            return await _give_back(
                job_id, session_factory=session_factory, interruptions=interruptions
            )
        reason = readable_error(exc)
        # The diagnostic half lives here and only here: the traceback, and for
        # an ObjectStoreError the keys and stderr its message carries. `reason`
        # is logged beside it so support can match a user's screen to a log line.
        logger.exception(
            "job %s: processing raised, marking failed (user sees: %s)", job_id, reason
        )
        async with session_factory() as session:
            try:
                await mark_failed(session, job_id, error=reason)
            except (JobNotFoundError, InvalidJobTransitionError) as write_exc:
                logger.error("job %s: could not record failure: %s", job_id, write_exc)
                return JobOutcome.SKIPPED
        logger.info("job %s: processing -> failed (%s)", job_id, reason)
        return JobOutcome.FAILED

    # 3. Record success. A ladder still to come is `pending`, not absent, so
    #    the page can say "HD processing" instead of offering nothing.
    async with session_factory() as session:
        try:
            await mark_done(
                session,
                job_id,
                output_key=result.output_key,
                hls_key=result.hls_key,
                hls_status=HlsStatus.PENDING if result.ladder_pending else None,
                width=result.width,
                height=result.height,
                duration_seconds=result.duration_seconds,
                thumbnail_key=result.thumbnail_key,
            )
        except (JobNotFoundError, InvalidJobTransitionError) as write_exc:
            logger.error("job %s: could not record completion: %s", job_id, write_exc)
            return JobOutcome.SKIPPED

    logger.info(
        "job %s: processing -> done (output_key=%s, hls_key=%s, ladder=%s)",
        job_id,
        result.output_key,
        result.hls_key or "none",
        "queued" if result.ladder_pending else "none",
    )

    # 4. Only after the commit: a ladder job that ran first would find the
    #    row still `processing` and skip it.
    if result.ladder_pending:
        await _hand_off_ladder(job_id, session_factory=session_factory, enqueue=enqueue_ladder_fn)
    return JobOutcome.DONE


async def _give_back(job_id: UUID, *, session_factory, interruptions: int) -> JobOutcome:
    """Put a stopped job back on the queue -- or, the third time, fail it."""
    async with session_factory() as session:
        try:
            if interruptions + 1 >= MAX_INTERRUPTIONS:
                await mark_failed(session, job_id, error=INTERRUPTED_TOO_OFTEN)
                logger.warning(
                    "job %s: interrupted %d times, failing it", job_id, interruptions + 1
                )
                return JobOutcome.FAILED
            await mark_interrupted(session, job_id)
        except (JobNotFoundError, InvalidJobTransitionError) as write_exc:
            logger.error("job %s: could not give the job back: %s", job_id, write_exc)
            return JobOutcome.SKIPPED
    logger.warning("job %s: worker stopping; processing -> queued (gave it back)", job_id)
    return JobOutcome.INTERRUPTED


async def _hand_off_ladder(job_id: UUID, *, session_factory, enqueue) -> None:
    try:
        await asyncio.to_thread(enqueue, job_id)
    except Exception:
        # The video is already done and playable; a ladder that cannot be
        # queued is settled now rather than left pending for the reaper.
        logger.exception("job %s: could not queue the HLS ladder; MP4 only", job_id)
        async with session_factory() as session:
            await mark_ladder_unavailable(session, job_id)


async def build_ladder_async(
    job_id: UUID,
    *,
    session_factory: async_sessionmaker[AsyncSession],
    ladder_step,
    interruptions: int = 0,
) -> HlsStatus | JobOutcome | None:
    """Build the adaptive ladder for a job that is already `done`.

    Never touches `status`: the video was finished when this was queued, and
    whatever happens here costs at most the quality selector. Returns the
    ladder state it settled on, or None if there was nothing to settle.
    """
    async with session_factory() as session:
        job = await get_job(session, job_id)
    if job is None or job.status != JobStatus.DONE.value or job.hls_status != HlsStatus.PENDING:
        # Deleted, or already settled by the reaper, since this was queued.
        logger.info("job %s: no ladder pending, dropping queue entry", job_id)
        return None

    # Sized from what the MP4 job probed and stored, rather than probing the
    # same file a second time.
    if job.height is None or job.width is None:
        logger.warning("job %s: no stored dimensions, so no ladder", job_id)
        async with session_factory() as session:
            await mark_ladder_unavailable(session, job_id)
        return HlsStatus.UNAVAILABLE

    probe = SourceProbe(
        width=job.width, height=job.height, duration_seconds=job.duration_seconds or 0.0
    )
    # An upload's ladder is cut from the original, not from the 720p MP4, so
    # a 4K upload keeps its detail. An edit's ladder is cut from the edit.
    ladder_input = job.source_key if getattr(job, "operations", None) is None else job.output_key
    logger.info("job %s: ladder pending -> building", job_id)
    try:
        hls_key = await asyncio.to_thread(
            ladder_step.run,
            job_id=job_id,
            source_key=ladder_input,
            probe=probe,
            max_fps=getattr(job, "max_fps", DEFAULT_MAX_FPS),
        )
    except Exception:
        if stop_requested() and interruptions + 1 < MAX_INTERRUPTIONS:
            # Stopped, not broken: the ladder stays pending and RQ runs it
            # again elsewhere. Anything already uploaded is half a ladder.
            logger.warning("job %s: worker stopping; the ladder goes back to its queue", job_id)
            with contextlib.suppress(Exception):
                await asyncio.to_thread(ladder_step.discard, job_id)
            return JobOutcome.INTERRUPTED
        logger.exception("job %s: HLS ladder failed; the MP4 stands", job_id)
        async with session_factory() as session:
            await mark_ladder_unavailable(session, job_id)
        return HlsStatus.UNAVAILABLE

    async with session_factory() as session:
        settled = await mark_ladder_ready(session, job_id, hls_key=hls_key)
    if settled is None:
        # Deleted, or given up on by the reaper, while this was building. The delete route
        # only finds segments through `hls_key`, which was never written, so
        # nobody else will ever remove these.
        logger.warning("job %s: ladder built but the job moved on; discarding it", job_id)
        try:
            await asyncio.to_thread(ladder_step.discard, job_id)
        except Exception:
            logger.exception("job %s: could not discard the unrecorded ladder", job_id)
        return None
    logger.info("job %s: ladder ready (hls_key=%s)", job_id, hls_key)
    if job.output_key:
        # After the ladder is recorded, so nothing here can hold it back.
        await asyncio.to_thread(after_ladder.run, job_id, job.output_key)
    return HlsStatus.READY


def process_job(job_id: str) -> str:
    """RQ entrypoint. Enqueued by the API as `app.worker.tasks.process_job`.

    Takes the job id as a string because that is what survives a round trip
    through the queue cleanly.
    """
    parsed = UUID(job_id)
    with stop_on_sigterm():
        outcome = asyncio.run(
            process_job_async(
                parsed,
                session_factory=get_worker_session_factory(),
                step=get_processing_step(),
                interruptions=_previous_interruptions(),
            )
        )
    # Returning a Retry is how RQ is asked to run this same queue entry again;
    # the row is already back in `queued` for the next worker to claim.
    return Retry(max=MAX_INTERRUPTIONS) if outcome is JobOutcome.INTERRUPTED else outcome.value


def build_ladder(job_id: str) -> str:
    """RQ entrypoint for the ladder queue. Enqueued by `process_job_async`."""
    with stop_on_sigterm():
        settled = asyncio.run(
            build_ladder_async(
                UUID(job_id),
                session_factory=get_worker_session_factory(),
                ladder_step=get_ladder_step(),
                interruptions=_previous_interruptions(),
            )
        )
    if settled is JobOutcome.INTERRUPTED:
        return Retry(max=MAX_INTERRUPTIONS)
    return settled.value if settled is not None else "skipped"


def help_encode(job_id: str) -> str:
    """RQ entrypoint for the pieces queue: help encode another job's MP4.

    A stop gives the piece in hand back and ends quietly; the coordinator,
    or another helper, encodes it. Nothing here touches the job's row.
    """
    from app.config import get_settings
    from app.queue import get_redis_connection
    from app.services.minio_client import bucket, internal_client
    from app.worker import chunked
    from app.worker.interruption import Interrupted
    from app.worker.storage import MinioObjectStore

    settings = get_settings()
    with stop_on_sigterm():
        try:
            encoded = chunked.help_encode(
                UUID(job_id),
                store=MinioObjectStore(internal_client(), bucket=bucket()),
                connection=get_redis_connection(),
                output_prefix=settings.worker_output_prefix,
                ffmpeg_binary=settings.worker_ffmpeg_binary,
                timeout_seconds=settings.worker_ffmpeg_timeout_seconds,
            )
        except (Interrupted, ObjectStoreError):
            if stop_requested():
                return "stopped; piece given back"
            raise
    return f"encoded {encoded} pieces"


def _previous_interruptions() -> int:
    """How many times RQ has already re-run this queue entry.

    Every re-run comes from a Retry returned above, so this counts
    interruptions; a job run directly, outside a worker, has had none.
    """
    current = get_current_job()
    return (current.number_of_retries or 0) if current is not None else 0
