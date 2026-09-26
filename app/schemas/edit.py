from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, StrictInt, model_validator


class _StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


FiniteNumber = Annotated[float, Field(strict=True, allow_inf_nan=False)] | StrictInt
PositiveStrictInt = Annotated[StrictInt, Field(gt=0)]
NonNegativeStrictInt = Annotated[StrictInt, Field(ge=0)]


class ClipParams(_StrictModel):
    start: FiniteNumber
    end: FiniteNumber

    @model_validator(mode="after")
    def end_follows_start(self):
        if self.start < 0:
            raise ValueError("clip start must be zero or greater")
        if self.end <= self.start:
            raise ValueError("clip end must be greater than start")
        return self


class CropParams(_StrictModel):
    x: NonNegativeStrictInt
    y: NonNegativeStrictInt
    w: PositiveStrictInt
    h: PositiveStrictInt


class ScaleParams(_StrictModel):
    height: PositiveStrictInt


class ConvertParams(_StrictModel):
    format: Literal["mkv", "mp3"]


class ClipOperation(_StrictModel):
    operation: Literal["clip"]
    params: ClipParams


class CropOperation(_StrictModel):
    operation: Literal["crop"]
    params: CropParams


class DownscaleOperation(_StrictModel):
    operation: Literal["downscale"]
    params: ScaleParams


class UpscaleOperation(_StrictModel):
    operation: Literal["upscale"]
    params: ScaleParams


class ConvertOperation(_StrictModel):
    operation: Literal["convert"]
    params: ConvertParams


EditOperation = Annotated[
    ClipOperation
    | CropOperation
    | DownscaleOperation
    | UpscaleOperation
    | ConvertOperation,
    Field(discriminator="operation"),
]


class EditRequest(_StrictModel):
    operations: Annotated[list[EditOperation], Field(min_length=1, max_length=5)]

    @model_validator(mode="after")
    def operations_are_unambiguous(self):
        names = [item.operation for item in self.operations]
        if len(names) != len(set(names)):
            raise ValueError("each operation may appear only once")
        if "downscale" in names and "upscale" in names:
            raise ValueError("downscale and upscale cannot be used together")
        return self

    def stored_operations(self) -> list[dict]:
        return [item.model_dump(mode="json") for item in self.operations]

    def operation(self, name: str):
        return next((item for item in self.operations if item.operation == name), None)
