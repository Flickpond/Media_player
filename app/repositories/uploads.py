"""Upload mutations share the caller's transaction and per-session row lock."""

from sqlalchemy import func, select

from app.errors import ApiNotFoundError
from app.models.job import Job
from app.models.upload import UploadSession
from app.models.user import User

MAX_OPEN_UPLOADS = 5
RESERVED_STATES = ("open", "completing", "aborting")


class UploadLimitReachedError(RuntimeError):
    pass


async def lock_owner(session, owner_id):
    # One owner lock serializes initiation and account deletion across API replicas.
    owner = await session.scalar(select(User).where(User.id == owner_id).with_for_update())
    return owner


async def get_upload(session, upload_id, owner_id):
    if await lock_owner(session, owner_id) is None:
        return None
    return await session.scalar(
        select(UploadSession)
        .where(UploadSession.id == upload_id, UploadSession.owner_id == owner_id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )


async def add_upload(session, upload):
    if await lock_owner(session, upload.owner_id) is None:
        raise ApiNotFoundError("not found")
    # Expired uploads reserve a slot until cleanup aborts their stored parts.
    count = await session.scalar(
        select(func.count()).select_from(UploadSession).where(
            UploadSession.owner_id == upload.owner_id,
            UploadSession.state.in_(RESERVED_STATES),
        )
    )
    if count >= MAX_OPEN_UPLOADS:
        raise UploadLimitReachedError
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
            max_fps=upload.max_fps,
            status="queued",
        )
    )
    upload.state = "ready"
    await session.commit()
