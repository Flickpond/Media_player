from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, StrictInt, model_serializer, model_validator

from app.services.media_rules import OPERATION_ORDER


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


# The colour sliders' ranges (sprint 6 plan, 2.1), as (low, high) per field.
# FFmpeg's eq would take wider ones; these are the ones the editor offers and
# the ones its preview was matched against.
ADJUST_RANGES: dict[str, tuple[float, float]] = {
    "brightness": (-0.3, 0.3),
    "contrast": (0.5, 2.0),
    "saturation": (0.0, 3.0),
    "gamma": (0.5, 2.0),
}


class AdjustParams(_StrictModel):
    """Colour adjustments. Every field is optional: one left out stays as it
    is. At least one has to be there -- an empty adjust is a request with
    nothing in it, and the panel never sends one."""

    brightness: FiniteNumber | None = None
    contrast: FiniteNumber | None = None
    saturation: FiniteNumber | None = None
    gamma: FiniteNumber | None = None

    @model_validator(mode="after")
    def values_are_in_range(self):
        given = {name: getattr(self, name) for name in ADJUST_RANGES}
        if all(value is None for value in given.values()):
            raise ValueError(
                "adjust needs at least one of brightness, contrast, saturation or gamma"
            )
        for name, value in given.items():
            low, high = ADJUST_RANGES[name]
            if value is not None and not low <= value <= high:
                raise ValueError(f"{name} must be between {low:g} and {high:g}")
        return self

    @model_serializer(mode="wrap")
    def _leave_out_what_was_left_out(self, handler):
        # Stored and handed to the worker as sent: a field the request left
        # out is absent, not null.
        return {name: value for name, value in handler(self).items() if value is not None}


class RotateParams(_StrictModel):
    degrees: Annotated[StrictInt, Field(ge=90, le=270, multiple_of=90)]


class FlipParams(_StrictModel):
    axis: Literal["horizontal", "vertical"]


class SpeedParams(_StrictModel):
    factor: Annotated[float, Field(strict=True, ge=0.5, le=2, multiple_of=0.25,
                                  allow_inf_nan=False)]


class SpeedOperation(_StrictModel):
    operation: Literal["speed"]
    params: SpeedParams


class RotateOperation(_StrictModel):
    operation: Literal["rotate"]
    params: RotateParams


class FlipOperation(_StrictModel):
    operation: Literal["flip"]
    params: FlipParams


class PresetParams(_StrictModel):
    name: Literal["bw", "warm", "cool", "vintage", "vivid"]


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


class AdjustOperation(_StrictModel):
    operation: Literal["adjust"]
    params: AdjustParams


class PresetOperation(_StrictModel):
    operation: Literal["preset"]
    params: PresetParams


EditOperation = Annotated[
    ClipOperation
    | CropOperation
    | DownscaleOperation
    | UpscaleOperation
    | RotateOperation
    | FlipOperation
    | SpeedOperation
    | ConvertOperation
    | PresetOperation
    | AdjustOperation,
    Field(discriminator="operation"),
]


class EditRequest(_StrictModel):
    # At most one of each, so never more than there are operations.
    operations: Annotated[
        list[EditOperation], Field(min_length=1, max_length=len(OPERATION_ORDER))
    ]

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
