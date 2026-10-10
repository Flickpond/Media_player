"""Real row-lock and transaction checks, isolated by a per-test owner."""

import asyncio
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
from app.repositories.jobs import list_source_keys
from app.repositories.uploads import add_upload, create_upload_job, get_upload
from app.schemas.upload import PART_SIZE

pytestmark = pytest.mark.skipif(
    os.getenv("RUN_POSTGRES_TESTS") != "1",
    reason="set RUN_POSTGRES_TESTS=1 to run tests that need the live compose stack",
)


@pytest.fixture
async def upload_db(owner):
    engine = create_async_engine(get_settings().postgres_dsn, poolclass=NullPool)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    now = datetime.now(UTC)
    uid = uuid4()
    upload = UploadSession(
        id=uid,
        owner_id=owner.id,
        job_id=uuid4(),
        filename="test.mp4",
        source_key=f"uploads/{uid}/test.mp4",
        size=20,
        part_size=PART_SIZE,
        state="completing",
        storage_id="test-storage-id",
        manifest=[{"n": 1, "etag": "x"}],
        created_at=now,
        expires_at=now + timedelta(hours=24),
    )
    async with factory() as session:
        await add_upload(session, upload)
    try:
        yield factory, upload
    finally:
        async with factory() as session:
            await session.execute(delete(UploadSession).where(UploadSession.id == uid))
            await session.commit()
        await engine.dispose()


async def test_pending_upload_protects_object_from_orphan_sweep(upload_db):
    factory, upload = upload_db
    async with factory() as session:
        assert upload.source_key in await list_source_keys(session)
        row = await get_upload(session, upload.id, upload.owner_id)
        row.state = "aborted"
        await session.commit()
        assert upload.source_key not in await list_source_keys(session)


async def test_concurrent_finalizers_create_one_job(upload_db):
    factory, upload = upload_db
    locked = asyncio.Event()
    attempting = asyncio.Event()
    release = asyncio.Event()

    async def first():
        async with factory() as session:
            row = await get_upload(session, upload.id, upload.owner_id)
            locked.set()
            await release.wait()
            await create_upload_job(session, row)

    async def second():
        await locked.wait()
        async with factory() as session:
            attempting.set()
            row = await get_upload(session, upload.id, upload.owner_id)
            assert row.state == "ready"
            # The API sees ready and only retries enqueue, never creates a row.

    one = asyncio.create_task(first())
    two = asyncio.create_task(second())
    try:
        await asyncio.wait_for(attempting.wait(), 5)
        await asyncio.sleep(0.05)
        assert not two.done()
    finally:
        release.set()
        await asyncio.wait_for(asyncio.gather(one, two), 10)
    async with factory() as session:
        jobs = (await session.scalars(select(Job).where(Job.id == upload.job_id))).all()
        assert len(jobs) == 1


async def test_owner_scope_and_rollback_preserve_session(upload_db):
    factory, upload = upload_db
    async with factory() as session:
        assert await get_upload(session, upload.id, uuid4()) is None
        row = await get_upload(session, upload.id, upload.owner_id)
        row.state = "aborting"
        await session.flush()
        await session.rollback()
    async with factory() as session:
        row = await get_upload(session, upload.id, upload.owner_id)
        assert row.state == "completing"
