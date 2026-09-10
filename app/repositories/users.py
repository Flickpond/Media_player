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
