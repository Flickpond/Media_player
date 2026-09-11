import asyncio
import logging
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, Query, Response, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import CurrentUser, OperatorUser
from app.database import get_session
from app.errors import ApiNotFoundError
from app.models.job import Job, JobStatus
from app.repositories.jobs import (
    DEFAULT_PAGE_SIZE,
    MAX_PAGE_SIZE,
    delete_job,
    get_job,
    list_jobs,
)
from app.schemas.job import ErrorResponse, JobResponse
from app.services.output_urls import OutputUrlSigner, get_output_url_signer
from app.services.storage import StorageService, get_storage_service

logger = logging.getLogger("app.api.jobs")

router = APIRouter(prefix="/jobs", tags=["jobs"])
# Separate route rather than a role branch inside GET /jobs. One URL that
# means different things depending on who asks is the kind of thing that
# passes its author's tests and surprises everyone else.
admin_router = APIRouter(prefix="/admin/jobs", tags=["admin"])
SessionDependency = Annotated[AsyncSession, Depends(get_session)]
SignerDependency = Annotated[OutputUrlSigner, Depends(get_output_url_signer)]
StorageDependency = Annotated[StorageService, Depends(get_storage_service)]


async def _to_response(job: Job, signer: OutputUrlSigner) -> JobResponse:
    output_url = None
    if job.status == JobStatus.DONE.value and job.output_key:
        output_url = await signer.create_url(job.output_key)

    error = job.error if job.status == JobStatus.FAILED.value else None
    return JobResponse(
        id=job.id,
        filename=job.filename,
        status=JobStatus(job.status),
        output_url=output_url,
        error=error,
    )


@router.get(
    "",
    response_model=list[JobResponse],
    response_model_exclude_none=True,
)
async def get_jobs(
    user: CurrentUser,
    session: SessionDependency,
    signer: SignerDependency,
    limit: Annotated[int, Query(ge=1, le=MAX_PAGE_SIZE)] = DEFAULT_PAGE_SIZE,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> list[JobResponse]:
    jobs = await list_jobs(session, owner_id=user.id, limit=limit, offset=offset)
    # Signed as a batch. Each signature is a local HMAC that still costs a hop
    # to the thread pool, so awaiting them one at a time made the endpoint's
    # latency the sum of every row's hop rather than the slowest one.
    return list(await asyncio.gather(*(_to_response(job, signer) for job in jobs)))


@router.get(
    "/{job_id}",
    response_model=JobResponse,
    response_model_exclude_none=True,
    responses={404: {"model": ErrorResponse}},
)
async def get_job_by_id(
    job_id: UUID,
    user: CurrentUser,
    session: SessionDependency,
    signer: SignerDependency,
) -> JobResponse:
    # Another owner's job is 404, never 403: a 403 confirms the id exists.
    job = await get_job(session, job_id, owner_id=user.id)
    if job is None:
        raise ApiNotFoundError("not found")
    return await _to_response(job, signer)


async def _delete_job_and_storage(
    session: AsyncSession,
    storage: StorageService,
    job_id: UUID,
    *,
    owner_id: UUID | None,
) -> Job | None:
    """Delete a job's row, then best-effort its storage objects.

    Deliberately in that order: what the caller sees (the job vanishing) does
    not depend on the storage half succeeding. A storage failure here leaves
    an orphaned object, exactly the case the reaper's sweep already exists to
    catch -- so this is a latency optimisation over waiting for that sweep,
    not a correctness requirement. Shared between the owner-scoped and
    operator routes; only how `owner_id` is passed to `delete_job` differs.
    """
    job = await delete_job(session, job_id, owner_id=owner_id)
    if job is None:
        return None

    for key in filter(None, (job.source_key, job.output_key)):
        try:
            await storage.delete_object(key)
        except Exception:
            logger.warning("job %s: could not delete storage object %s", job_id, key)

    return job


@router.delete(
    "/{job_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    responses={404: {"model": ErrorResponse}},
)
async def delete_job_by_id(
    job_id: UUID,
    user: CurrentUser,
    session: SessionDependency,
    storage: StorageDependency,
) -> Response:
    """Delete an upload -- for an accidental upload or general cleanup.

    Works regardless of status: queued, processing, done or failed can all be
    deleted. A job mid-processing is not a special case -- see the note on
    `delete_job` for why the worker already tolerates the row disappearing
    underneath it.
    """
    job = await _delete_job_and_storage(session, storage, job_id, owner_id=user.id)
    if job is None:
        raise ApiNotFoundError("not found")
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@admin_router.get(
    "",
    response_model=list[JobResponse],
    response_model_exclude_none=True,
)
async def get_all_jobs(
    _operator: OperatorUser,
    session: SessionDependency,
    signer: SignerDependency,
    limit: Annotated[int, Query(ge=1, le=MAX_PAGE_SIZE)] = DEFAULT_PAGE_SIZE,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> list[JobResponse]:
    """Every job, for spotting stuck ones. Operators only.

    This is sprint 1's operator story, which used to be what GET /jobs did for
    everybody. It keeps the unscoped query -- `owner_id=None` -- and puts a
    role in front of it.
    """
    jobs = await list_jobs(session, owner_id=None, limit=limit, offset=offset)
    return list(await asyncio.gather(*(_to_response(job, signer) for job in jobs)))


@admin_router.delete(
    "/{job_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    responses={404: {"model": ErrorResponse}},
)
async def delete_job_by_id_as_operator(
    job_id: UUID,
    _operator: OperatorUser,
    session: SessionDependency,
    storage: StorageDependency,
) -> Response:
    """Same as `DELETE /jobs/{id}`, but unscoped -- any user's job.

    Operators only, and it exists for the same reason `GET /admin/jobs`
    does: identifying and cleaning up uploads that are not the operator's
    own. `delete_job(owner_id=None)` is the same "any owner" convention
    `get_all_jobs` already uses.
    """
    job = await _delete_job_and_storage(session, storage, job_id, owner_id=None)
    if job is None:
        raise ApiNotFoundError("not found")
    return Response(status_code=status.HTTP_204_NO_CONTENT)
