import re
from pathlib import PurePosixPath
from typing import Annotated
from uuid import uuid4

from fastapi import APIRouter, Depends, File, UploadFile
from fastapi.responses import JSONResponse
from sqlalchemy.ext.asyncio import AsyncSession
from starlette.concurrency import run_in_threadpool

from app.database import get_session
from app.queue import enqueue_job
from app.repositories.jobs import create_job
from app.services.media_type import SNIFF_LENGTH, sniff_video_type
from app.services.storage import StorageService, get_storage_service

router = APIRouter(tags=["uploads"])

MAX_FILE_SIZE = 100 * 1024 * 1024

# The formats the pipeline is meant to handle. This is the cheap first gate on
# what the client *claims*; `sniff_video_type` is the one that decides what the
# file *is*. Both are needed: the declared type is free to check and rejects
# the obvious cases before any bytes are examined, but it is attacker-supplied
# -- a browser fills it in from the file extension, so a renamed PDF arrives
# declared `video/mp4` and only the content check catches it.
ALLOWED_CONTENT_TYPES = frozenset(
    {
        "video/mp4",
        "video/mpeg",
        "video/quicktime",
        "video/webm",
        "video/x-matroska",
        "video/x-msvideo",
    }
)

DEFAULT_FILENAME = "upload.bin"
MAX_FILENAME_LENGTH = 100
_UNSAFE_FILENAME_CHARS = re.compile(r"[^A-Za-z0-9._-]")

StorageDependency = Annotated[
    StorageService,
    Depends(get_storage_service),
]

SessionDependency = Annotated[
    AsyncSession,
    Depends(get_session),
]


def safe_filename(raw: str | None) -> str:
    """Reduce a client-supplied filename to something safe to put in a key.

    The name comes from the multipart headers, so it is attacker-controlled:
    `../` segments, control characters and unbounded length all arrive intact.
    Object keys are opaque strings to S3, so a `..` is not a traversal today --
    what this enforces is that everything belonging to job X stays under
    `uploads/X/`, here rather than in whatever the storage backend happens to
    normalise. The original name is still kept on the row for display.
    """
    name = PurePosixPath(raw or "").name
    name = _UNSAFE_FILENAME_CHARS.sub("_", name).lstrip(".")
    return name[:MAX_FILENAME_LENGTH] or DEFAULT_FILENAME


@router.post("/upload", status_code=202)
async def upload_video(
    file: Annotated[UploadFile, File(...)],
    storage: StorageDependency,
    session: SessionDependency,
):
    # Strip any `; charset=...` parameter before matching: the type is what
    # decides whether this is storable, the parameters are not.
    content_type = (file.content_type or "").split(";")[0].strip().lower()
    if content_type not in ALLOWED_CONTENT_TYPES:
        return JSONResponse(
            status_code=415,
            content={"error": "unsupported media type"},
        )

    # By the time this runs Starlette has already spooled the whole part to a
    # temp file, so this measures bytes that have landed rather than preventing
    # them from landing -- that is the Content-Length guard in `app.main`'s job.
    # This stays as the backstop for a request that understated its length.
    file.file.seek(0, 2)
    size = file.file.tell()
    file.file.seek(0)

    if size > MAX_FILE_SIZE:
        return JSONResponse(
            status_code=413,
            content={"error": "file too large"},
        )

    # What the bytes say, which is the only account of the file that the client
    # cannot write. Stored in place of the declared type, so the object is
    # always served back as what it actually is -- the two are allowed to
    # disagree, since browsers routinely mislabel a container by extension.
    head = file.file.read(SNIFF_LENGTH)
    file.file.seek(0)
    sniffed_type = sniff_video_type(head)
    if sniffed_type is None:
        return JSONResponse(
            status_code=415,
            content={"error": "file content is not a recognized video format"},
        )

    job_id = uuid4()

    filename = safe_filename(file.filename)

    source_key = f"uploads/{job_id}/{filename}"

    await storage.upload_stream(
        source_key,
        file.file,
        length=size,
        content_type=sniffed_type,
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
