import asyncio
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, Query
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_session
from app.errors import ApiNotFoundError
from app.models.job import Job, JobStatus
from app.repositories.jobs import DEFAULT_PAGE_SIZE, MAX_PAGE_SIZE, get_job, list_jobs
from app.schemas.job import ErrorResponse, JobResponse
from app.services.output_urls import OutputUrlSigner, get_output_url_signer

router = APIRouter(prefix="/jobs", tags=["jobs"])
SessionDependency = Annotated[AsyncSession, Depends(get_session)]
SignerDependency = Annotated[OutputUrlSigner, Depends(get_output_url_signer)]


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
    session: SessionDependency,
    signer: SignerDependency,
    limit: Annotated[int, Query(ge=1, le=MAX_PAGE_SIZE)] = DEFAULT_PAGE_SIZE,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> list[JobResponse]:
    jobs = await list_jobs(session, limit=limit, offset=offset)
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
    session: SessionDependency,
    signer: SignerDependency,
) -> JobResponse:
    job = await get_job(session, job_id)
    if job is None:
        raise ApiNotFoundError("not found")
    return await _to_response(job, signer)
