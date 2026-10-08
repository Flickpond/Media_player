"""Durable upload sessions, separate from jobs until content is validated."""

from datetime import datetime
from uuid import UUID

from sqlalchemy import BigInteger, CheckConstraint, DateTime, ForeignKey, Integer, Text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base


class UploadSession(Base):
    __tablename__ = "upload_sessions"
    __table_args__ = (
        CheckConstraint("size > 0 AND part_size > 0", name="ck_upload_size"),
        CheckConstraint(
            "state IN ('open', 'completing', 'ready', 'completed', 'aborting', 'aborted')",
            name="ck_upload_state",
        ),
    )

    id: Mapped[UUID] = mapped_column(primary_key=True)
    owner_id: Mapped[UUID] = mapped_column(ForeignKey("users.id"), index=True)
    filename: Mapped[str] = mapped_column(Text)
    source_key: Mapped[str] = mapped_column(Text, unique=True)
    storage_id: Mapped[str | None] = mapped_column(Text)
    size: Mapped[int] = mapped_column(BigInteger)
    part_size: Mapped[int] = mapped_column(Integer)
    state: Mapped[str] = mapped_column(Text, default="open")
    manifest: Mapped[list | None] = mapped_column(JSONB)
    # Reserved at initiation, but there is no job row until validation succeeds.
    job_id: Mapped[UUID] = mapped_column(unique=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)

    @property
    def part_count(self) -> int:
        return (self.size + self.part_size - 1) // self.part_size
