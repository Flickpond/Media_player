"""Track C's source-aware checks, called by B before constructing FFmpeg args.

These functions do no I/O and never change the supplied parameters. The probe
must describe the downloaded input file, not metadata sent by the browser.
Only these curated ValueError messages should be exposed by the edit processor;
ordinary exceptions still belong in the operator log.
"""

import math

from app.worker.probe import SourceProbe


def validate_crop(params: dict, probe: SourceProbe) -> None:
    """Require a positive rectangle wholly inside the original video frame."""
    if not isinstance(params, dict) or set(params) != {"x", "y", "w", "h"}:
        raise ValueError("crop requires x, y, w and h in source pixels")

    # bool is an int subclass, and strings may be FFmpeg expressions. Neither
    # is a pixel coordinate; coercion here would change what the user selected.
    if any(type(params[key]) is not int for key in ("x", "y", "w", "h")):
        raise ValueError("crop coordinates and dimensions must be whole numbers")
    x, y, w, h = (params[key] for key in ("x", "y", "w", "h"))
    if x < 0 or y < 0:
        raise ValueError("crop x and y must be zero or greater")
    if w <= 0 or h <= 0:
        raise ValueError("crop width and height must be greater than zero")

    if any(type(size) is not int or size <= 0 for size in (probe.width, probe.height)):
        raise ValueError("the source video's dimensions could not be determined")
    if x + w > probe.width or y + h > probe.height:
        raise ValueError("crop must fit within the source video's frame")


def _finite_number(value: object) -> bool:
    if type(value) not in (int, float):
        return False
    try:
        return math.isfinite(value)
    except OverflowError:
        return False


def validate_clip(params: dict, probe: SourceProbe) -> None:
    """Require 0 <= start < end <= the original duration, in seconds."""
    if not isinstance(params, dict) or set(params) != {"start", "end"}:
        raise ValueError("clip requires start and end in seconds")
    start, end = params["start"], params["end"]
    if not _finite_number(start) or not _finite_number(end):
        raise ValueError("clip start and end must be finite numbers")
    if start < 0:
        raise ValueError("clip start must be zero or greater")
    if end <= start:
        raise ValueError("clip end must be greater than start")
    if not _finite_number(probe.duration_seconds) or probe.duration_seconds <= 0:
        raise ValueError("the source video's duration could not be determined")
    if end > probe.duration_seconds:
        raise ValueError("clip end must not exceed the source video's duration")
