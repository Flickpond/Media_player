"""Ownership against a real database.

The unit tests prove the endpoints ask for the right owner. These prove the
database actually enforces it -- the scoped query, the foreign key, and the
role constraint. Between them, "a user sees only their own jobs" is covered
from both ends.
"""

import os
import uuid

import pytest
import pytest_asyncio
from sqlalchemy import delete, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from app.config import get_settings
from app.models.job import Job
from app.models.user import LEGACY_USER_EMAIL, User, UserRole
from app.repositories.jobs import create_job, get_job, list_jobs
from app.repositories.users import (
    EmailAlreadyRegisteredError,
    create_user,
    get_user_by_email,
)

pytestmark = pytest.mark.skipif(
    os.getenv("RUN_POSTGRES_TESTS") != "1",
    reason="set RUN_POSTGRES_TESTS=1 to run tests that need the live compose stack",
)


@pytest_asyncio.fixture
async def session_factory():
    engine = create_async_engine(get_settings().postgres_dsn, poolclass=NullPool)
    yield async_sessionmaker(engine, expire_on_commit=False)
    await engine.dispose()


@pytest_asyncio.fixture
async def two_owners(session_factory):
    """Two real accounts, cleaned up with everything they own."""
    made = []
    async with session_factory() as session:
        for _ in range(2):
            made.append(
                await create_user(
                    session,
                    email=f"owner-{uuid.uuid4().hex[:10]}@example.test",
                    password_hash="not-a-real-hash",
                )
            )
    yield made
    async with session_factory() as session:
        ids = [u.id for u in made]
        await session.execute(delete(Job).where(Job.owner_id.in_(ids)))
        await session.execute(delete(User).where(User.id.in_(ids)))
        await session.commit()


# --- scoping ---------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_listing_scoped_to_one_owner_excludes_the_other(session_factory, two_owners):
    mine, theirs = two_owners
    async with session_factory() as session:
        for owner in (mine, theirs):
            await create_job(
                session,
                owner_id=owner.id,
                filename="demo.mp4",
                source_key=f"uploads/{uuid.uuid4()}/demo.mp4",
            )

    async with session_factory() as session:
        listed = await list_jobs(session, owner_id=mine.id, limit=200, offset=0)

    owners = {job.owner_id for job in listed}
    # Membership, not counts: the table is shared with a running stack (T-12).
    assert owners == {mine.id}, "a scoped listing must contain exactly one owner"
    assert theirs.id not in owners


@pytest.mark.asyncio
async def test_get_job_returns_none_for_another_owner(session_factory, two_owners):
    """None is what lets the endpoint answer 404 instead of 403."""
    mine, theirs = two_owners
    async with session_factory() as session:
        job = await create_job(
            session,
            owner_id=theirs.id,
            filename="theirs.mp4",
            source_key=f"uploads/{uuid.uuid4()}/theirs.mp4",
        )

    async with session_factory() as session:
        assert await get_job(session, job.id, owner_id=mine.id) is None
        assert await get_job(session, job.id, owner_id=theirs.id) is not None
        # No owner means the worker and the reaper, which act on any job.
        assert await get_job(session, job.id) is not None


@pytest.mark.asyncio
async def test_the_unscoped_listing_spans_owners(session_factory, two_owners):
    """The operator view. `owner_id=None` is deliberate, not an oversight."""
    mine, theirs = two_owners
    async with session_factory() as session:
        for owner in (mine, theirs):
            await create_job(
                session,
                owner_id=owner.id,
                filename="demo.mp4",
                source_key=f"uploads/{uuid.uuid4()}/demo.mp4",
            )

    async with session_factory() as session:
        listed = await list_jobs(session, owner_id=None, limit=200, offset=0)

    owners = {job.owner_id for job in listed}
    assert {mine.id, theirs.id} <= owners


# --- what the database itself refuses --------------------------------------


@pytest.mark.asyncio
async def test_a_duplicate_email_is_refused_by_the_index_not_a_prior_check(session_factory):
    """Two registrations racing would both pass a SELECT; only one can insert."""
    email = f"dup-{uuid.uuid4().hex[:10]}@example.test"
    async with session_factory() as session:
        first = await create_user(session, email=email, password_hash="x")
    try:
        async with session_factory() as session:
            with pytest.raises(EmailAlreadyRegisteredError):
                await create_user(session, email=email, password_hash="y")
    finally:
        async with session_factory() as session:
            await session.execute(delete(User).where(User.id == first.id))
            await session.commit()


@pytest.mark.asyncio
async def test_an_invalid_role_is_refused_by_the_check_constraint(session_factory):
    """Constrained in the database, matching how jobs.status is done."""
    async with session_factory() as session:
        with pytest.raises(IntegrityError):
            await session.execute(
                text(
                    "INSERT INTO users (id, email, password_hash, role) "
                    "VALUES (gen_random_uuid(), :email, 'x', 'superuser')"
                ),
                {"email": f"bad-role-{uuid.uuid4().hex[:8]}@example.test"},
            )
            await session.commit()
        await session.rollback()


@pytest.mark.asyncio
async def test_a_job_cannot_point_at_a_user_that_does_not_exist(session_factory):
    """The foreign key is what stops an orphaned owner_id."""
    async with session_factory() as session:
        with pytest.raises(IntegrityError):
            await create_job(
                session,
                owner_id=uuid.uuid4(),
                filename="orphan.mp4",
                source_key=f"uploads/{uuid.uuid4()}/orphan.mp4",
            )
        await session.rollback()


# --- the migration's backfill ----------------------------------------------


@pytest.mark.asyncio
async def test_the_legacy_owner_exists_and_cannot_be_logged_into(session_factory):
    """Rows predating accounts were backfilled to a sentinel.

    It has to exist, so `owner_id` could be made NOT NULL, and it has to be
    unusable, so nobody can sign in as the owner of everyone's history.
    """
    from app.services.security import verify_password

    async with session_factory() as session:
        legacy = await get_user_by_email(session, LEGACY_USER_EMAIL)

    assert legacy is not None, "the migration must leave a sentinel owner behind"
    assert legacy.role == UserRole.USER.value
    assert not verify_password("", legacy.password_hash)
    assert not verify_password("password", legacy.password_hash)


@pytest.mark.asyncio
async def test_every_job_has_an_owner(session_factory):
    """NOT NULL is enforced in the schema; this proves the deployed data agrees."""
    async with session_factory() as session:
        result = await session.execute(text("SELECT count(*) FROM jobs WHERE owner_id IS NULL"))
        assert result.scalar_one() == 0
