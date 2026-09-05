from typing import Annotated
from uuid import uuid4

from fastapi import APIRouter, Depends, File, UploadFile
from fastapi.responses import JSONResponse
from sqlalchemy.ext.asyncio import AsyncSession
from starlette.concurrency import run_in_threadpool

from app.database import get_session
from app.queue import enqueue_job
from app.repositories.jobs import create_job
from app.services.storage import StorageService, get_storage_service

router = APIRouter(tags=["uploads"])

MAX_FILE_SIZE = 100 * 1024 * 1024

StorageDependency = Annotated[
    StorageService,
    Depends(get_storage_service),
]

SessionDependency = Annotated[
    AsyncSession,
    Depends(get_session),
]


@router.post("/upload", status_code=202)
async def upload_video(
    file: Annotated[UploadFile, File(...)],
    storage: StorageDependency,
    session: SessionDependency,
):
    # Determine file size without loading the whole file into memory.
    file.file.seek(0, 2)
    size = file.file.tell()
    file.file.seek(0)

    if size > MAX_FILE_SIZE:
        return JSONResponse(
            status_code=400,
            content={"error": "file too large"},
        )

    job_id = uuid4()

    filename = file.filename or "upload.bin"

    source_key = f"uploads/{job_id}/{filename}"

    await storage.upload_stream(
        source_key,
        file.file,
        length=size,
        content_type=file.content_type or "application/octet-stream",
    )

    await create_job(
        session,
        job_id=job_id,
        filename=filename,
        source_key=source_key,
    )

    # Enqueue last, and only after the row exists: a worker can pick the job up
    # the instant this returns, and it reads source_key from that row. Redis is
    # sub-millisecond but the client is blocking, so it goes to a thread like
    # the storage calls above -- the upload must stay under 1s (N1).
    await run_in_threadpool(enqueue_job, job_id)

    return {
        "job_id": str(job_id),
    }
