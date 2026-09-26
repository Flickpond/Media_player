import asyncio
import logging
from pathlib import PurePosixPath
from typing import Annotated
from uuid import UUID, uuid4

from fastapi import APIRouter, Depends, Query, Response, status
from fastapi.responses import JSONResponse
from sqlalchemy.ext.asyncio import AsyncSession
from starlette.concurrency import run_in_threadpool

from app.api.deps import CurrentUser, OperatorUser
from app.config import get_settings
from app.database import get_session
from app.errors import ApiNotFoundError
from app.models.job import Job, JobStatus
from app.queue import enqueue_job
from app.repositories.jobs import (
    DEFAULT_PAGE_SIZE,
    MAX_PAGE_SIZE,
    InvalidJobTransitionError,
    JobNotFoundError,
    create_edit_job,
    delete_job,
    get_job,
    list_jobs,
    prepare_retry,
)
from app.schemas.edit import EditRequest
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

    # Not a presigned object URL like `output_url`: a ladder is hundreds of
    # objects, so this points at the API route that signs each part on
    # demand behind the owner check. The prefix comes from settings because
    # nginx strips `/api/` before the app sees it -- the app's own routing
    # table does not know the path the browser used.
    hls_url = None
    if job.status == JobStatus.DONE.value and job.hls_key:
        prefix = get_settings().api_public_prefix.rstrip("/")
        hls_url = f"{prefix}/jobs/{job.id}/hls/master.m3u8"

    error = job.error if job.status == JobStatus.FAILED.value else None
    return JobResponse(
        id=job.id,
        filename=job.filename,
        status=JobStatus(job.status),
        output_url=output_url,
        hls_url=hls_url,
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


@router.post(
    "/{job_id}/retry",
    response_model=dict[str, str],
    status_code=status.HTTP_202_ACCEPTED,
    responses={
        404: {"model": ErrorResponse},
        409: {"model": ErrorResponse},
        503: {"model": ErrorResponse},
    },
)
async def retry_job_by_id(
    job_id: UUID,
    user: CurrentUser,
    session: SessionDependency,
) -> dict[str, str] | Response:
    """Retry the caller's failed job using its existing source and operations."""
    try:
        await prepare_retry(session, job_id, owner_id=user.id)
    except JobNotFoundError as exc:
        raise ApiNotFoundError("not found") from exc
    except InvalidJobTransitionError:
        return JSONResponse(status_code=409, content={"error": "only failed jobs can be retried"})

    try:
        await run_in_threadpool(enqueue_job, job_id)
        await session.commit()
    except Exception:
        await session.rollback()
        logger.exception("job %s: retry could not be confirmed", job_id)
        return JSONResponse(
            status_code=503,
            content={"error": "retry could not be confirmed; refresh the job and try again"},
        )

    logger.info("job %s: failed -> queued (retry requested)", job_id)
    return {"job_id": str(job_id)}


@router.post(
    "/{job_id}/edit",
    response_model=dict[str, str],
    status_code=status.HTTP_202_ACCEPTED,
    responses={
        404: {"model": ErrorResponse},
        409: {"model": ErrorResponse},
        503: {"model": ErrorResponse},
    },
)
async def edit_job_by_id(
    job_id: UUID,
    request: EditRequest,
    user: CurrentUser,
    session: SessionDependency,
) -> dict[str, str] | Response:
    """Create one queued batch edit from a completed job owned by the caller."""
    source = await get_job(session, job_id, owner_id=user.id)
    if source is None:
        raise ApiNotFoundError("not found")
    if source.status != JobStatus.DONE.value or not source.output_key:
        return JSONResponse(
            status_code=409,
            content={"error": "only completed jobs can be edited"},
        )

    convert = request.operation("convert")
    extension = convert.params.format if convert is not None else "mp4"
    stem = PurePosixPath(source.filename).stem or "edited-video"
    new_job_id = uuid4()
    created = await create_edit_job(
        session,
        job_id=new_job_id,
        owner_id=user.id,
        filename=f"{stem}-edited.{extension}",
        source_key=source.output_key,
        operations=request.stored_operations(),
    )

    try:
        await run_in_threadpool(enqueue_job, created.id)
    except Exception:
        # The row has no storage of its own yet. Removing it avoids leaving a
        # permanently queued library entry when Redis refused the delivery.
        await delete_job(session, created.id, owner_id=user.id)
        logger.exception("job %s: edit could not be queued", created.id)
        return JSONResponse(
            status_code=503,
            content={"error": "edit could not be queued; please try again"},
        )

    return {"job_id": str(created.id)}


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

    # An edit borrows another job's output as its source. Deleting the edit
    # must not delete that shared object and break the original library item.
    keys = list(filter(None, (job.output_key,)))
    if job.operations is None:
        keys.insert(0, job.source_key)

    # An HLS ladder is hundreds of objects under one prefix, so deleting
    # `hls_key` alone would remove the master playlist and orphan every
    # segment it pointed at -- invisible, and growing with each delete.
    if job.hls_key:
        prefix = job.hls_key.rsplit("/", 1)[0] + "/"
        try:
            keys.extend(name for name, _ in await storage.list_objects(prefix))
        except Exception:
            logger.warning("job %s: could not list HLS objects under %s", job_id, prefix)

    for key in keys:
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
