"""The browser contract for direct uploads; storage identifiers stay private."""

from typing import Annotated, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, StrictInt, model_validator

PART_SIZE = 16 * 1024 * 1024
MAX_PARTS = 10000


class UploadModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class StartUpload(UploadModel):
    filename: str = Field(min_length=1, max_length=255)
    size: Annotated[StrictInt, Field(gt=0, le=PART_SIZE * MAX_PARTS)]
    content_type: str = Field(min_length=1, max_length=128)


PartNumber = Annotated[StrictInt, Field(ge=1, le=MAX_PARTS)]


class SignParts(UploadModel):
    part_numbers: list[PartNumber] = Field(min_length=1, max_length=100)

    @model_validator(mode="after")
    def unique_numbers(self):
        if len(set(self.part_numbers)) != len(self.part_numbers):
            raise ValueError("part numbers must be unique")
        return self


class UploadedPart(UploadModel):
    n: PartNumber
    etag: str = Field(min_length=1, max_length=128)


class CompleteUpload(UploadModel):
    parts: list[UploadedPart] = Field(min_length=1, max_length=MAX_PARTS)

    @model_validator(mode="after")
    def contiguous_parts(self):
        numbers = sorted(part.n for part in self.parts)
        if numbers != list(range(1, len(numbers) + 1)):
            raise ValueError("parts must contain each number exactly once, starting at 1")
        return self

    def manifest(self) -> list[dict]:
        return [part.model_dump() for part in sorted(self.parts, key=lambda part: part.n)]


class UploadStarted(UploadModel):
    upload_id: UUID
    part_size: int
    part_count: int


class PartUrls(UploadModel):
    urls: dict[str, str]


class UploadProgress(UploadModel):
    parts_done: list[UploadedPart]
    state: Literal["open", "completing", "ready", "completed", "aborting", "aborted"]
    part_size: int
    part_count: int
    job_id: UUID | None


class UploadCompleted(UploadModel):
    job_id: UUID
