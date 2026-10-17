from uuid import UUID

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.user import User, UserRole


class EmailAlreadyRegisteredError(RuntimeError):
    pass


async def create_user(
    session: AsyncSession,
    *,
    email: str,
    password_hash: str,
    role: UserRole = UserRole.USER,
) -> User:
    user = User(email=email.strip().lower(), password_hash=password_hash, role=role.value)
    session.add(user)
    try:
        await session.commit()
    except IntegrityError as exc:
        # The unique index is what actually decides this, not a prior SELECT --
        # two registrations racing on the same address would both pass a check
        # and only one can win the insert.
        await session.rollback()
        raise EmailAlreadyRegisteredError(email) from exc
    await session.refresh(user)
    return user


async def get_user_by_email(session: AsyncSession, email: str) -> User | None:
    result = await session.execute(select(User).where(User.email == email.strip().lower()))
    return result.scalar_one_or_none()


async def get_user(session: AsyncSession, user_id: UUID) -> User | None:
    return await session.get(User, user_id)


async def list_users_with_job_counts(session, *, limit=50, offset=0):
    from sqlalchemy import func

    from app.models.job import Job

    result = await session.execute(
        select(User.id, User.email, User.role, func.count(Job.id).label("job_count"))
        .outerjoin(Job, Job.owner_id == User.id)
        .group_by(User.id)
        .order_by(User.created_at.desc(), User.id)
        .limit(limit)
        .offset(offset)
    )
    return result.mappings().all()


async def account_contents(session, user_id):
    from app.models.job import Job
    from app.models.upload import UploadSession

    # lock_owner has already blocked new uploads and FK inserts for this account.
    uploads = list((await session.scalars(
        select(UploadSession).where(UploadSession.owner_id == user_id).with_for_update()
    )).all())
    jobs = list((await session.scalars(
        select(Job).where(Job.owner_id == user_id).order_by(Job.id).with_for_update()
    )).all())
    return jobs, uploads


async def delete_account_rows(session, user_id):
    from sqlalchemy import delete

    from app.models.job import Job
    from app.models.upload import UploadSession

    # The caller commits once, only after every storage operation has succeeded.
    await session.execute(delete(UploadSession).where(UploadSession.owner_id == user_id))
    await session.execute(delete(Job).where(Job.owner_id == user_id))
    await session.execute(delete(User).where(User.id == user_id))
