from datetime import datetime
from uuid import UUID

from sqlalchemy import delete, func, select, update
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
    owner_id: UUID,
    filename: str,
    source_key: str,
    job_id: UUID | None = None,
) -> Job:
    job = Job(
        owner_id=owner_id,
        filename=filename,
        source_key=source_key,
        status=JobStatus.QUEUED.value,
        **({"id": job_id} if job_id is not None else {}),
    )
    session.add(job)
    await session.commit()
    await session.refresh(job)
    return job


async def get_job(
    session: AsyncSession, job_id: UUID, *, owner_id: UUID | None = None
) -> Job | None:
    """Fetch a job, optionally requiring an owner.

    `owner_id=None` means "any owner" and is for the worker and the reaper,
    which act on jobs regardless of who uploaded them. API callers always pass
    one, and a mismatch returns None so the endpoint can answer 404 -- a 403
    would confirm the id exists and let someone probe for valid ids.
    """
    job = await session.get(Job, job_id)
    if job is None or (owner_id is not None and job.owner_id != owner_id):
        return None
    return job


async def list_jobs(
    session: AsyncSession,
    *,
    owner_id: UUID | None = None,
    limit: int = DEFAULT_PAGE_SIZE,
    offset: int = 0,
) -> list[Job]:
    """`owner_id=None` returns every owner's jobs -- the operator view.

    Scoping happens in the query, not by filtering afterwards: filtering a page
    after fetching it silently shrinks the page and breaks pagination.
    """
    statement = select(Job)
    if owner_id is not None:
        statement = statement.where(Job.owner_id == owner_id)
    # `id` breaks ties on `created_at`. Without it two rows written in the same
    # transaction have no defined order between pages, so one can appear on
    # both sides of a boundary while another appears on neither.
    statement = statement.order_by(Job.created_at.desc(), Job.id).limit(limit).offset(offset)
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


async def delete_job(
    session: AsyncSession, job_id: UUID, *, owner_id: UUID | None = None
) -> Job | None:
    """Delete a job, returning the deleted row -- or `None` if there was
    nothing to delete.

    `owner_id=None` means "any owner", the same convention as `get_job` and
    `list_jobs`: for the operator route, which can delete a job it does not
    own. API callers acting as a regular user always pass one, and a
    mismatch returns `None` so the endpoint can answer 404 -- indistinguishable
    from an unknown id, the same as `get_job`, so a delete attempt cannot be
    used to probe which ids exist.

    Returning the row rather than a bare bool is what lets the caller clean
    up `source_key` and `output_key` in object storage without a second
    query. Safe regardless of the job's current status: the worker's own
    writes (`mark_processing`/`mark_done`/`mark_failed`) are already
    conditional updates that treat a missing row as `JobNotFoundError` --
    the same tolerance the reaper depends on -- so a job mid-flight simply
    loses its race for a row that is no longer there, rather than
    corrupting one that is.
    """
    statement = delete(Job).where(Job.id == job_id)
    if owner_id is not None:
        statement = statement.where(Job.owner_id == owner_id)
    statement = statement.returning(Job)
    result = await session.execute(statement)
    job = result.scalar_one_or_none()
    if job is None:
        await session.rollback()
        return None
    await session.commit()
    return job


async def list_source_keys(session: AsyncSession) -> set[str]:
    result = await session.execute(select(Job.source_key))
    return set(result.scalars().all())


async def list_stale(
    session: AsyncSession,
    *,
    status: JobStatus,
    before: datetime,
) -> list[Job]:
    statement = (
        select(Job)
        .where(Job.status == status.value, Job.updated_at < before)
        .order_by(Job.updated_at)
    )
    result = await session.execute(statement)
    return list(result.scalars().all())


async def mark_stale_failed(session: AsyncSession, job_id: UUID) -> Job | None:
    statement = (
        update(Job)
        .where(Job.id == job_id, Job.status == JobStatus.PROCESSING.value)
        .values(
            status=JobStatus.FAILED.value,
            error="worker stopped responding; job was not completed",
            updated_at=func.now(),
        )
        .returning(Job)
    )
    result = await session.execute(statement)
    job = result.scalar_one_or_none()
    if job is None:
        await session.rollback()
        return None
    await session.commit()
    return job
