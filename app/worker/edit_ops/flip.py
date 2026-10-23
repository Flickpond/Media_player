"""Mirror the rotated picture on its horizontal or vertical axis."""

from app.services.media_rules import Frame
from app.worker.edit_ops.registry import FilterStep, register_filter


def build(params: dict, _frame: Frame) -> FilterStep:
    return FilterStep(video=("hflip" if params["axis"] == "horizontal" else "vflip",))


register_filter("flip", build)
