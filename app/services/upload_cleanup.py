"""Run periodically: python -m app.services.upload_cleanup.

Expired sessions are locked and cleaned without ever deleting a created job.
Storage lifecycle rules separately handle an initiation whose response was lost.
"""

import asyncio
import logging
from datetime import UTC, datetime

from sqlalchemy import select

from app.database import get_session_factory
from app.models.upload import UploadSession
from app.repositories.uploads import get_upload
from app.services.multipart_storage import get_multipart_storage

logger = logging.getLogger(__name__)


async def cleanup_expired(factory=None, store=None):
    factory = factory or get_session_factory()
    store = store or get_multipart_storage()
    count = 0
    async with factory() as session:
        candidates = (
            await session.execute(
                select(UploadSession.id, UploadSession.owner_id).where(
                    UploadSession.expires_at <= datetime.now(UTC),
                    UploadSession.state.in_(["open", "completing", "aborting"]),
                )
            )
        ).all()
        await session.rollback()
        for upload_id, owner_id in candidates:
            try:
                upload = await get_upload(session, upload_id, owner_id)
                if upload is None or upload.state not in {"open", "completing", "aborting"}:
                    await session.rollback()
                    continue
                upload.state = "aborting"
                # Keep the lock over storage so complete cannot race cleanup.
                await store.abort(upload)
                upload.state = "aborted"
                await session.commit()
                count += 1
            except Exception:
                await session.rollback()
                logger.exception("could not clean expired upload %s", upload_id)
    return count


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    logger.info("cleaned %s expired uploads", asyncio.run(cleanup_expired()))
