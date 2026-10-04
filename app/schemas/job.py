from uuid import UUID

from pydantic import BaseModel, ConfigDict

from app.models.job import HlsStatus, JobStatus


class JobResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    filename: str
    status: JobStatus
    output_url: str | None = None
    # The adaptive ladder's master playlist, when one was built. Absent on
    # every job uploaded before sprint 3 and on any job whose ladder failed,
    # so the player treats it as optional and falls back to `output_url`.
    hls_url: str | None = None
    # Always present, so the page never has to infer the ladder's state from
    # a missing `hls_url`: "pending" is what lets it say HD is still coming.
    hls_status: HlsStatus
    # What the source actually is, once probed. Omitted until then, and on
    # every job from before sprint 4.
    width: int | None = None
    height: int | None = None
    duration_seconds: float | None = None
    error: str | None = None


class ErrorResponse(BaseModel):
    error: str
