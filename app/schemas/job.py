from uuid import UUID

from pydantic import BaseModel, ConfigDict

from app.models.job import JobStatus


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
    error: str | None = None


class ErrorResponse(BaseModel):
    error: str
