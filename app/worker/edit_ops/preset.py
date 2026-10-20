"""Filter presets: one-click looks the browser can preview before processing.

The five names are the contract in docs/contract.md (sprint 6). Each chain is
deliberately built from eq/colorbalance/curves: those filters run after the HDR
tone-map, so a preset means the same thing on every upload, and the browser's
WebGL preview can mirror the same saturation, contrast, lift and tint.

The browser's 32-cube colour tables are generated from these exact chains by
frontend/scripts/generate-preset-luts.py. Regenerate them after changing a
chain: a manifest test rejects tables made from different worker filters.
The tables include FFmpeg's vintage curve instead of trying to reproduce it
with a sepia shader. Interpolation and video encoding still make the preview
an approximation.
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
    # eq works in YUV: the same visible RGB colour otherwise desaturates
    # differently in BT.601 and BT.709 (including tone-mapped HDR). Convert
    # the matrix, not just its tag, before applying a look. Convert back to
    # browser-playable 4:2:0 after RGB filters such as curves/colorbalance.
    matrix = "scale=in_color_matrix=auto:out_color_matrix=bt709:out_range=tv"
    tags = "setparams=colorspace=bt709:range=limited"
    return FilterStep(
        video=(
            matrix,
            "format=yuv444p",
            tags,
            *PRESET_FILTERS[params["name"]],
            matrix,
            "format=yuv420p",
            tags,
        )
    )


register_filter("preset", build)
