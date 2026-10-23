"""Colour adjustments: brightness, contrast, saturation and gamma.

FFmpeg's `eq` filter, after the tone-map (registry.TONE_MAPPED_FROM), so it
always works on the standard 8-bit picture -- the same one the editor's
preview filters in the browser (frontend/adjust.js imitates `eq` step for
step: docs/c-sprint6-colour.md). The parameters are `AdjustParams` in
app/schemas/edit.py, whose ranges are the sliders'; the rule leaves the frame
as it is.

`eq`'s own defaults are the "leave it alone" values, so a field the request
left out -- or sent at its default -- is simply not passed.
"""

from app.services.media_rules import Frame
from app.worker.edit_ops.registry import FilterStep, register_filter

# In eq's option order. A field at its default changes nothing.
DEFAULTS: dict[str, float] = {"brightness": 0.0, "contrast": 1.0, "saturation": 1.0, "gamma": 1.0}


def _number(value: float) -> str:
    # Four significant figures is finer than a slider step; `g` drops the
    # trailing zeros FFmpeg would otherwise be handed (1.2, not 1.2000).
    return format(float(value), ".4g")


def build(params: dict, _frame: Frame) -> FilterStep:
    options = [
        f"{name}={_number(params[name])}"
        for name, default in DEFAULTS.items()
        if params.get(name) is not None and params[name] != default
    ]
    if not options:
        return FilterStep()
    return FilterStep(video=("eq=" + ":".join(options),))


register_filter("adjust", build)
