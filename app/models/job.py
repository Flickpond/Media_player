from datetime import datetime
from enum import StrEnum
from uuid import UUID, uuid4

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    Text,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.dialects.postgresql import UUID as PostgreSQLUUID
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base


class JobStatus(StrEnum):
    QUEUED = "queued"
    PROCESSING = "processing"
    DONE = "done"
    FAILED = "failed"


class HlsStatus(StrEnum):
    """Where a job's adaptive ladder stands, as the player needs to know it.

    `READY` exactly when `hls_key` exists -- the database enforces that.
    `UNAVAILABLE` covers every way of having no ladder: finished without
    one, failed, or uploaded before ladders existed.
    """

    PENDING = "pending"
    READY = "ready"
    UNAVAILABLE = "unavailable"


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
        CheckConstraint(
            "hls_status IN ('pending', 'ready', 'unavailable')",
            name="ck_jobs_hls_status",
        ),
        # The two can't drift apart: a ladder is ready exactly when its key
        # exists.
        CheckConstraint(
            "(hls_status = 'ready') = (hls_key IS NOT NULL)",
            name="ck_jobs_hls_ready_has_key",
        ),
        CheckConstraint(
            "(width IS NULL OR width > 0) AND (height IS NULL OR height > 0) "
            "AND (duration_seconds IS NULL OR duration_seconds > 0)",
            name="ck_jobs_media_dimensions_positive",
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
    hls_status: Mapped[str] = mapped_column(
        Text,
        nullable=False,
        default=HlsStatus.PENDING.value,
        server_default=text("'pending'"),
    )
    # What the source actually is, once the worker has probed it. Null until
    # then -- and on every job from before sprint 4.
    width: Mapped[int | None] = mapped_column(Integer, nullable=True)
    height: Mapped[int | None] = mapped_column(Integer, nullable=True)
    duration_seconds: Mapped[float | None] = mapped_column(Float, nullable=True)
    thumbnail_key: Mapped[str | None] = mapped_column(Text, nullable=True)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
