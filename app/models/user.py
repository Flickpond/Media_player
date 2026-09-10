from datetime import datetime
from enum import StrEnum
from uuid import UUID, uuid4

from sqlalchemy import CheckConstraint, DateTime, Index, Text, func, text
from sqlalchemy.dialects.postgresql import UUID as PostgreSQLUUID
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base


class UserRole(StrEnum):
    USER = "user"
    OPERATOR = "operator"


# The owner every pre-authorization job is backfilled to. Jobs uploaded before
# there were accounts have to belong to someone, and inventing a NULL branch
# that every scoped query then has to remember is worse than one obviously fake
# row. Its password hash is unusable, so nobody can log in as it.
LEGACY_USER_EMAIL = "legacy@flickpond.invalid"


class User(Base):
    __tablename__ = "users"
    __table_args__ = (
        # Constrained in the database rather than only in Python, matching how
        # jobs.status is done. The database is the last line of defence here and
        # should stay that way.
        CheckConstraint("role IN ('user', 'operator')", name="ck_users_role"),
        Index("ix_users_email", "email", unique=True),
    )

    id: Mapped[UUID] = mapped_column(PostgreSQLUUID(as_uuid=True), primary_key=True, default=uuid4)
    email: Mapped[str] = mapped_column(Text, nullable=False)
    password_hash: Mapped[str] = mapped_column(Text, nullable=False)
    role: Mapped[str] = mapped_column(
        Text, nullable=False, default=UserRole.USER.value, server_default=text("'user'")
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
