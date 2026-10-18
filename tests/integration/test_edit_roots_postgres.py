"""Edits start from the original upload: finding it against real rows."""

import os
from uuid import uuid4

import pytest
import pytest_asyncio
from sqlalchemy import delete
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from app.config import get_settings
from app.models.job import Job, JobStatus
from app.repositories.jobs import resolve_edit_roots

pytestmark = pytest.mark.skipif(
    os.getenv("RUN_POSTGRES_TESTS") != "1",
    reason="set RUN_POSTGRES_TESTS=1 to run PostgreSQL integration tests",
)


@pytest_asyncio.fixture
async def session_factory():
    engine = create_async_engine(get_settings().postgres_dsn, poolclass=NullPool)
    yield async_sessionmaker(engine, expire_on_commit=False)
    await engine.dispose()


def _done(owner, *, source_key, output_key, operations=None):
    # An upload leaves `operations` out entirely, as create_job does: passing
    # None would store a JSON null, which ck_jobs_operations_nonempty_array
    # rightly refuses.
    extra = {"operations": operations} if operations is not None else {}
    return Job(
        id=uuid4(),
        owner_id=owner,
        filename="clip.mp4",
        status=JobStatus.DONE.value,
        source_key=source_key,
        output_key=output_key,
        hls_status="unavailable",
        height=2160,
        width=3840,
        **extra,
    )


@pytest.mark.asyncio
async def test_every_kind_of_edit_finds_its_original(session_factory, owner) -> None:
    clip = [{"operation": "clip", "params": {"start": 0, "end": 1}}]

    def key(kind):
        return f"{kind}/{uuid4()}/clip.mp4"

    upload = _done(owner.id, source_key=key("uploads"), output_key=key("outputs"))
    # Since sprint 5: an edit shares its upload's source.
    new_edit = _done(
        owner.id, source_key=upload.source_key, output_key=key("outputs"), operations=clip
    )
    # Before: an edit's source was its parent's output -- two hops here.
    old_edit = _done(
        owner.id, source_key=upload.output_key, output_key=key("outputs"), operations=clip
    )
    old_edit_of_edit = _done(
        owner.id, source_key=old_edit.output_key, output_key=key("outputs"), operations=clip
    )
    # Its upload deleted: nothing to walk back to.
    orphan = _done(owner.id, source_key=key("outputs"), output_key=key("outputs"), operations=clip)
    rows = [upload, new_edit, old_edit, old_edit_of_edit, orphan]
    try:
        async with session_factory() as session:
            session.add_all(rows)
            await session.commit()
            roots = await resolve_edit_roots(session, rows)

        assert roots[upload.id].id == upload.id
        assert roots[new_edit.id].id == upload.id
        assert roots[old_edit.id].id == upload.id
        assert roots[old_edit_of_edit.id].id == upload.id
        assert roots[orphan.id].id == orphan.id
    finally:
        async with session_factory() as cleanup:
            await cleanup.execute(delete(Job).where(Job.id.in_([row.id for row in rows])))
            await cleanup.commit()
