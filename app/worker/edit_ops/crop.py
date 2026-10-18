"""Crop: keep a rectangle of the picture, in the original's pixels.

The reference operation for the seam (registry.py). Its parameters are
`CropParams` in app/schemas/edit.py; its rule is `_crop_rule` in
app/services/media_rules.py; the box fitting the frame is checked against the
real file by app.worker.validation.validate_crop.
"""

from app.services.media_rules import Frame
from app.worker.edit_ops.registry import FilterStep, register_filter


def build(params: dict, _frame: Frame) -> FilterStep:
    return FilterStep(video=(f"crop={params['w']}:{params['h']}:{params['x']}:{params['y']}",))


register_filter("crop", build)
