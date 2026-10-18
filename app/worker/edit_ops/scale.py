"""Downscale and upscale: change the picture's height, keeping its shape.

`-2` lets FFmpeg pick the width, rounded to even, which libx264 needs. An
upscale uses lanczos: it invents detail, and lanczos invents the least blur.
"""

from app.services.media_rules import Frame
from app.worker.edit_ops.registry import FilterStep, register_filter


def downscale(params: dict, _frame: Frame) -> FilterStep:
    return FilterStep(video=(f"scale=-2:{params['height']}",))


def upscale(params: dict, _frame: Frame) -> FilterStep:
    return FilterStep(video=(f"scale=-2:{params['height']}:flags=lanczos",))


register_filter("downscale", downscale)
register_filter("upscale", upscale)
