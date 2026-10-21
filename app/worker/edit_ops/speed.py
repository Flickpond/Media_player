"""Retime the clipped video and audio together, preserving the audio pitch."""

from app.services.media_rules import Frame
from app.worker.edit_ops.registry import FilterStep, register_filter


def build(params: dict, _frame: Frame) -> FilterStep:
    factor = params["factor"]
    # atempo changes tempo without changing pitch. Each supported factor is
    # inside its native 0.5..2 range, so no resampling or asetrate is needed.
    return FilterStep(
        video=(f"setpts=(PTS-STARTPTS)/{factor}",),
        audio=("asetpts=PTS-STARTPTS", f"atempo={factor}"),
    )


register_filter("speed", build)
