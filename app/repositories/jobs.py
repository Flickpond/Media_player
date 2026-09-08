from uuid import UUID

from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.job import Job, JobStatus

# Page sizes for `list_jobs`. A default rather than "everything" because the
# endpoint's cost grows with the table: each row returned is a signed URL to
# mint. The cap is what stops a caller asking for the whole table anyway.
DEFAULT_PAGE_SIZE = 50
MAX_PAGE_SIZE = 200


class JobNotFoundError(LookupError):
    pass


class InvalidJobTransitionError(RuntimeError):
    pass


async def create_job(
    session: AsyncSession,
    *,
    filename: str,
    source_key: str,
    job_id: UUID | None = None,
) -> Job:
    job = Job(
        filename=filename,
        source_key=source_key,
        status=JobStatus.QUEUED.value,
        **({"id": job_id} if job_id is not None else {}),
    )
    session.add(job)
    await session.commit()
    await session.refresh(job)
    return job


async def get_job(session: AsyncSession, job_id: UUID) -> Job | None:
    return await session.get(Job, job_id)


async def list_jobs(
    session: AsyncSession,
    *,
    limit: int = DEFAULT_PAGE_SIZE,
    offset: int = 0,
) -> list[Job]:
    # `id` breaks ties on `created_at`. Without it two rows written in the same
    # transaction have no defined order between pages, so one can appear on
    # both sides of a boundary while another appears on neither.
    statement = select(Job).order_by(Job.created_at.desc(), Job.id).limit(limit).offset(offset)
    result = await session.execute(statement)
    return list(result.scalars().all())


async def _transition(
    session: AsyncSession,
    *,
    job_id: UUID,
    expected_status: JobStatus,
    next_status: JobStatus,
    output_key: str | None = None,
    error: str | None = None,
) -> Job:
    values: dict[str, object | None] = {
        "status": next_status.value,
        "output_key": output_key,
        "error": error,
        "updated_at": func.now(),
    }
    statement = (
        update(Job)
        .where(Job.id == job_id, Job.status == expected_status.value)
        .values(**values)
        .returning(Job)
    )
    result = await session.execute(statement)
    job = result.scalar_one_or_none()
    if job is not None:
        await session.commit()
        return job

    await session.rollback()
    existing = await get_job(session, job_id)
    if existing is None:
        raise JobNotFoundError(str(job_id))
    raise InvalidJobTransitionError(
        f"cannot transition job {job_id} from {existing.status} to {next_status.value}"
    )


async def mark_processing(session: AsyncSession, job_id: UUID) -> Job:
    return await _transition(
        session,
        job_id=job_id,
        expected_status=JobStatus.QUEUED,
        next_status=JobStatus.PROCESSING,
    )


async def mark_done(session: AsyncSession, job_id: UUID, *, output_key: str) -> Job:
    if not output_key.strip():
        raise ValueError("output_key must not be empty")
    return await _transition(
        session,
        job_id=job_id,
        expected_status=JobStatus.PROCESSING,
        next_status=JobStatus.DONE,
        output_key=output_key,
    )


async def mark_failed(session: AsyncSession, job_id: UUID, *, error: str) -> Job:
    if not error.strip():
        raise ValueError("failed jobs require a readable error")
    return await _transition(
        session,
        job_id=job_id,
        expected_status=JobStatus.PROCESSING,
        next_status=JobStatus.FAILED,
        error=error.strip(),
    )
