from datetime import datetime
from enum import StrEnum
from uuid import UUID, uuid4

from sqlalchemy import CheckConstraint, DateTime, ForeignKey, Index, Text, func, text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.dialects.postgresql import UUID as PostgreSQLUUID
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base


class JobStatus(StrEnum):
    QUEUED = "queued"
    PROCESSING = "processing"
    DONE = "done"
    FAILED = "failed"


class Job(Base):
    __tablename__ = "jobs"
    __table_args__ = (
        CheckConstraint(
            "status IN ('queued', 'processing', 'done', 'failed')",
            name="ck_jobs_status",
        ),
        CheckConstraint(
            "(status = 'done' AND output_key IS NOT NULL) "
            "OR (status <> 'done' AND output_key IS NULL)",
            name="ck_jobs_output_key",
        ),
        CheckConstraint(
            "(status = 'failed' AND error IS NOT NULL AND btrim(error) <> '') "
            "OR (status <> 'failed' AND error IS NULL)",
            name="ck_jobs_error",
        ),
        # Shape only. "This job is an edit" and "this job was asked to do
        # nothing" are different things, and only the first is storable.
        # What is *in* the array is the worker's registry to validate.
        CheckConstraint(
            "operations IS NULL OR "
            "(jsonb_typeof(operations) = 'array' AND jsonb_array_length(operations) > 0)",
            name="ck_jobs_operations_nonempty_array",
        ),
        Index("ix_jobs_status", "status"),
        Index("ix_jobs_owner_id", "owner_id"),
        Index("ix_jobs_created_at", "created_at"),
    )

    id: Mapped[UUID] = mapped_column(PostgreSQLUUID(as_uuid=True), primary_key=True, default=uuid4)
    owner_id: Mapped[UUID] = mapped_column(
        PostgreSQLUUID(as_uuid=True), ForeignKey("users.id"), nullable=False
    )
    filename: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[str] = mapped_column(
        Text, nullable=False, default=JobStatus.QUEUED.value, server_default=text("'queued'")
    )
    source_key: Mapped[str] = mapped_column(Text, nullable=False)
    output_key: Mapped[str | None] = mapped_column(Text, nullable=True)
    # The HLS master playlist, when the worker managed to build a ladder.
    # Null on a plain `done` job is legitimate, not a missing write -- the
    # ladder is best-effort and the MP4 in `output_key` is the fallback.
    hls_key: Mapped[str | None] = mapped_column(Text, nullable=True)
    # Null on an upload; on an edit job, what the caller asked for. The
    # array's order is not execution order -- the worker has a fixed one.
    operations: Mapped[list[dict] | None] = mapped_column(JSONB, nullable=True)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
