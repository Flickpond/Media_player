"""Account deletion transactions against PostgreSQL; no storage services required."""

import os
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from app.config import get_settings
from app.models.job import Job
from app.models.upload import UploadSession
from app.models.user import User
from app.repositories.uploads import add_upload, lock_owner
from app.repositories.users import account_contents, delete_account_rows, list_users_with_job_counts
from app.schemas.upload import PART_SIZE

pytestmark = pytest.mark.skipif(
    os.getenv("RUN_POSTGRES_TESTS") != "1",
    reason="set RUN_POSTGRES_TESTS=1 to run tests that need the live compose stack",
)


async def test_account_rows_roll_back_together_then_delete_together(owner):
    engine = create_async_engine(get_settings().postgres_dsn, poolclass=NullPool)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    now = datetime.now(UTC)
    uid = uuid4()
    upload = UploadSession(
        id=uid, owner_id=owner.id, job_id=uuid4(), filename="account.mp4",
        source_key=f"uploads/{uid}/account.mp4", size=20, part_size=PART_SIZE,
        state="open", created_at=now, expires_at=now + timedelta(hours=24),
    )
    jid = uuid4()
    try:
        async with factory() as session:
            await add_upload(session, upload)
            session.add(Job(id=jid, owner_id=owner.id, filename="account.mp4",
                            source_key=f"uploads/{jid}/account.mp4", status="queued"))
            await session.commit()
        async with factory() as session:
            assert await lock_owner(session, owner.id) is not None
            jobs, uploads = await account_contents(session, owner.id)
            assert {j.id for j in jobs} == {jid}
            assert {u.id for u in uploads} == {uid}
            listed = await list_users_with_job_counts(session, limit=200)
            assert next(u for u in listed if u["id"] == owner.id)["job_count"] == 1
            await delete_account_rows(session, owner.id)
            await session.rollback()
        async with factory() as session:
            assert await session.get(User, owner.id) is not None
            assert await session.get(Job, jid) is not None
            assert await session.get(UploadSession, uid) is not None
            await lock_owner(session, owner.id)
            await delete_account_rows(session, owner.id)
            await session.commit()
        async with factory() as session:
            assert await session.get(User, owner.id) is None
            assert await session.scalar(select(Job.id).where(Job.owner_id == owner.id)) is None
            assert await session.get(UploadSession, uid) is None
    finally:
        async with factory() as session:
            await session.execute(delete(UploadSession).where(UploadSession.id == uid))
            await session.commit()
        await engine.dispose()
