"""Upload mutations share the caller's transaction and per-session row lock."""

from sqlalchemy import select

from app.models.job import Job
from app.models.upload import UploadSession


async def get_upload(session, upload_id, owner_id):
    return await session.scalar(
        select(UploadSession)
        .where(UploadSession.id == upload_id, UploadSession.owner_id == owner_id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )


async def add_upload(session, upload):
    session.add(upload)
    await session.commit()


async def create_upload_job(session, upload):
    # The row lock plus this transaction makes one session produce one job.
    session.add(
        Job(
            id=upload.job_id,
            owner_id=upload.owner_id,
            filename=upload.filename,
            source_key=upload.source_key,
            status="queued",
        )
    )
    upload.state = "ready"
    await session.commit()
