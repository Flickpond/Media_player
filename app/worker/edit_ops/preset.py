"""Filter presets: one-click looks the browser can preview before processing.

The five names are the contract in docs/contract.md (sprint 6). Each chain is
deliberately built from eq/colorbalance/curves: those filters run after the HDR
tone-map, so a preset means the same thing on every upload, and the browser's
WebGL preview can mirror the same saturation, contrast, lift and tint.

`vintage` uses FFmpeg's own curve rather than a hand-written one. Its exact
shape is part of libavfilter, and the browser preview is an approximation of
it, just as every "live preview is the browser's, the result is FFmpeg's"
feature in this sprint is.
"""

from app.services.media_rules import Frame
from app.worker.edit_ops.registry import FilterStep, register_filter

PRESET_FILTERS: dict[str, tuple[str, ...]] = {
    "bw": ("eq=saturation=0",),
    "warm": (
        "eq=saturation=1.12:brightness=0.02",
        "colorbalance=rm=0.08:bm=-0.08",
    ),
    "cool": (
        "eq=saturation=1.05:brightness=0.01",
        "colorbalance=rm=-0.06:bm=0.08",
    ),
    "vintage": (
        "eq=saturation=0.82:contrast=0.95",
        "curves=preset=vintage",
    ),
    "vivid": (
        "eq=saturation=1.38:contrast=1.12",
        "colorbalance=rm=0.03:bm=0.03",
    ),
}


def build(params: dict, _frame: Frame) -> FilterStep:
    return FilterStep(video=PRESET_FILTERS[params["name"]])


register_filter("preset", build)
