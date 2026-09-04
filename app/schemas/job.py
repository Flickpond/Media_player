from uuid import UUID

from pydantic import BaseModel, ConfigDict

from app.models.job import JobStatus


class JobResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    filename: str
    status: JobStatus
    output_url: str | None = None
    error: str | None = None


class ErrorResponse(BaseModel):
    error: str
