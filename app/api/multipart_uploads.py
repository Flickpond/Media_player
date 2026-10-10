"""Owner-scoped resumable uploads. A row lock serializes completion and abort."""

import logging
from datetime import UTC, datetime, timedelta
from typing import Annotated
from uuid import UUID, uuid4

from fastapi import APIRouter, Depends, HTTPException, Response
from fastapi.exceptions import RequestValidationError
from fastapi.routing import APIRoute
from starlette.concurrency import run_in_threadpool

from app.api.deps import CurrentUser, SessionDependency
from app.api.uploads import ALLOWED_CONTENT_TYPES, safe_filename
from app.config import Settings, get_settings
from app.errors import ApiForbiddenError, ApiNotFoundError, ApiUnauthorizedError
from app.models.upload import UploadSession
from app.queue import enqueue_job
from app.repositories.uploads import add_upload, create_upload_job, get_upload
from app.schemas.upload import (
    PART_SIZE,
    CompleteUpload,
    PartUrls,
    SignParts,
    StartUpload,
    UploadCompleted,
    UploadProgress,
    UploadStarted,
)
from app.services.media_type import sniff_video_type
from app.services.multipart_storage import MultipartStorage, get_multipart_storage

logger = logging.getLogger(__name__)


class UploadRoute(APIRoute):
    def get_route_handler(self):
        handler = super().get_route_handler()

        async def handle(request):
            from fastapi.responses import JSONResponse

            try:
                return await handler(request)
            except HTTPException as exc:
                return JSONResponse({"error": exc.detail}, status_code=exc.status_code)
            except (
                ApiNotFoundError,
                ApiUnauthorizedError,
                ApiForbiddenError,
                RequestValidationError,
            ):
                raise
            except Exception:
                logger.exception("multipart request could not be confirmed")
                return JSONResponse(
                    {"error": "upload could not be confirmed; refresh and try again"},
                    status_code=503,
                )

        return handle


router = APIRouter(prefix="/uploads", tags=["uploads"], route_class=UploadRoute)
Storage = Annotated[MultipartStorage, Depends(get_multipart_storage)]


async def owned(session, upload_id, owner_id):
    upload = await get_upload(session, upload_id, owner_id)
    if upload is None:
        raise ApiNotFoundError("not found")
    return upload


def active(upload):
    if upload.expires_at <= datetime.now(UTC):
        raise HTTPException(410, "upload expired; start a new upload")
    if upload.state != "open":
        raise HTTPException(409, "upload no longer accepts parts")


@router.post("", status_code=201, response_model=UploadStarted)
async def start(
    request: StartUpload,
    user: CurrentUser,
    session: SessionDependency,
    store: Storage,
    settings: Annotated[Settings, Depends(get_settings)],
):
    if request.size > settings.max_upload_bytes:
        raise HTTPException(
            413, f"upload exceeds maximum size of {settings.max_upload_bytes} bytes"
        )
    if request.content_type.split(";")[0].strip().lower() not in ALLOWED_CONTENT_TYPES:
        raise HTTPException(415, "unsupported media type")
    now = datetime.now(UTC)
    job_id = uuid4()
    filename = safe_filename(request.filename)
    upload = UploadSession(
        id=uuid4(),
        owner_id=user.id,
        job_id=job_id,
        filename=filename,
        source_key=f"uploads/{job_id}/{filename}",
        size=request.size,
        part_size=PART_SIZE,
        state="open",
        created_at=now,
        expires_at=now + timedelta(hours=24),
    )
    # Persist the key first, so a storage/DB interruption cannot leave a completed
    # object unprotected from the orphan sweep. Abandoned parts have a lifecycle rule.
    await add_upload(session, upload)
    upload = await owned(session, upload.id, user.id)
    upload.storage_id = await store.initiate(upload.source_key)
    await session.commit()
    return {
        "upload_id": str(upload.id),
        "part_size": upload.part_size,
        "part_count": upload.part_count,
    }


@router.post("/{upload_id}/parts", response_model=PartUrls)
async def sign_parts(
    upload_id: UUID,
    request: SignParts,
    user: CurrentUser,
    session: SessionDependency,
    store: Storage,
):
    upload = await owned(session, upload_id, user.id)
    active(upload)
    if not upload.storage_id:
        raise HTTPException(409, "upload initialization is incomplete; start a new upload")
    if max(request.part_numbers) > upload.part_count:
        raise HTTPException(422, "part number exceeds this upload's part count")
    expires = max(1, min(900, int((upload.expires_at - datetime.now(UTC)).total_seconds())))
    return {"urls": await store.sign(upload, request.part_numbers, expires)}


def validate_parts(upload, actual):
    expected = upload.manifest
    if len(actual) != upload.part_count or len(expected) != upload.part_count:
        raise HTTPException(422, "all parts must be uploaded before completion")
    for supplied, stored in zip(expected, sorted(actual, key=lambda p: p["n"]), strict=True):
        size = min(upload.part_size, upload.size - (stored["n"] - 1) * upload.part_size)
        if (
            supplied["n"] != stored["n"]
            or supplied["etag"] != stored["etag"]
            or stored["size"] != size
        ):
            raise HTTPException(422, "part ETags and sizes must match the uploaded parts")


async def discard(session, upload, store):
    # Durable intent prevents signing or completing while cleanup is retried.
    upload.state = "aborting"
    await session.commit()
    upload = await owned(session, upload.id, upload.owner_id)
    await store.abort(upload)
    upload.state = "aborted"
    await session.commit()


@router.post("/{upload_id}/complete", status_code=202, response_model=UploadCompleted)
async def complete(
    upload_id: UUID,
    request: CompleteUpload,
    user: CurrentUser,
    session: SessionDependency,
    store: Storage,
):
    upload = await owned(session, upload_id, user.id)
    if upload.state == "completed":
        return {"job_id": str(upload.job_id)}
    if upload.state in {"aborting", "aborted"}:
        raise HTTPException(409, "upload was cancelled")
    if upload.state == "open":
        active(upload)
        if not upload.storage_id:
            raise HTTPException(409, "upload initialization is incomplete")
        upload.manifest = request.manifest()
        validate_parts(upload, await store.parts(upload))
        upload.state = "completing"
        # Freeze the manifest before calling S3. If the response is lost, a
        # retry can HEAD the unique key and continue without completing twice.
        await session.commit()
        upload = await owned(session, upload_id, user.id)
    if upload.state == "completing":
        if upload.manifest != request.manifest():
            raise HTTPException(409, "completion already started with different parts")
        head = await store.head(upload.source_key)
        if head is None:
            await store.complete(upload)
            head = await store.head(upload.source_key)
        if head is None:
            raise HTTPException(503, "uploaded object is not available; retry completion")
        if head["ContentLength"] != upload.size:
            await discard(session, upload, store)
            raise HTTPException(422, "uploaded size does not match the declared size")
        if sniff_video_type(await store.sniff(upload.source_key)) is None:
            await discard(session, upload, store)
            raise HTTPException(415, "file content is not a recognized video format")
        # Keep safe octet-stream + attachment metadata. The worker probes the
        # real file and sets the derived media type on its processed output.
        await create_upload_job(session, upload)
        upload = await owned(session, upload_id, user.id)
    if upload.state == "ready":
        # The job is committed before enqueue; a worker can start immediately.
        # A lost Redis acknowledgement may redeliver this same ID. The existing
        # worker's conditional claim handles duplicates; the reaper recovers loss.
        await run_in_threadpool(enqueue_job, upload.job_id)
        upload.state = "completed"
        await session.commit()
    if upload.state != "completed":
        raise HTTPException(409, "upload cannot be completed in its current state")
    return {"job_id": str(upload.job_id)}


@router.get("/{upload_id}", response_model=UploadProgress)
async def resume(upload_id: UUID, user: CurrentUser, session: SessionDependency, store: Storage):
    upload = await owned(session, upload_id, user.id)
    if upload.state == "open":
        active(upload)
        parts = await store.parts(upload) if upload.storage_id else []
        parts = [{"n": p["n"], "etag": p["etag"]} for p in parts]
    else:
        parts = upload.manifest or []
    return {
        "parts_done": parts,
        "state": upload.state,
        "part_size": upload.part_size,
        "part_count": upload.part_count,
        "job_id": str(upload.job_id) if upload.state in {"ready", "completed"} else None,
    }


@router.delete("/{upload_id}", status_code=204)
async def abort(upload_id: UUID, user: CurrentUser, session: SessionDependency, store: Storage):
    upload = await owned(session, upload_id, user.id)
    if upload.state in {"ready", "completed"}:
        raise HTTPException(409, "upload already created a job; delete the job instead")
    if upload.state == "completing":
        raise HTTPException(409, "completion has started; retry completion")
    if upload.state != "aborted":
        await discard(session, upload, store)
    return Response(status_code=204)
