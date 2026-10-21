"""Clockwise turns, applied after the crop in original-source coordinates."""

from app.services.media_rules import Frame
from app.worker.edit_ops.registry import FilterStep, register_filter

_FILTERS = {90: ("transpose=1",), 180: ("hflip", "vflip"), 270: ("transpose=2",)}


def build(params: dict, _frame: Frame) -> FilterStep:
    return FilterStep(video=_FILTERS[params["degrees"]])


register_filter("rotate", build)
