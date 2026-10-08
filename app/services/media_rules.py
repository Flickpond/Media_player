"""Track C's media rules: what a source may be, and which edits make sense for it.

Two audiences read these rules, and they must read the same ones:

* The **worker** applies them to what ffprobe actually found -- the duration
  limit to every upload, before any encoding, and the scale rules to an edit's
  real input.
* The **API** applies the scale rules to the dimensions already stored on the
  job, so an impossible request is a 422 at once instead of a failed job a
  minute later, and hands the same rules to the page as `edit_options` so the
  editor never offers something the API would refuse.

Pure functions, no I/O, nothing imported from `app.worker` -- the API process
imports this module, and it should not have to import the worker to do so.
Every refusal is a `MediaRuleError` whose message is written for the person
who uploaded the video: it is shown to them verbatim.
"""

import math
from collections.abc import Iterable

# The heights an edit may scale to. Fixed rungs rather than any number: an odd
# height is one libx264 refuses mid-encode, and a free-form number is not
# something the editor's dropdowns can offer anyway. 2160p is the top because
# it is the top of the ladder -- an upscale past it would be stored and then
# never played at its own size.
SCALE_HEIGHTS: tuple[int, ...] = (240, 360, 480, 720, 1080, 1440, 2160)
MAX_SCALE_HEIGHT = SCALE_HEIGHTS[-1]

# The operations that change the picture. MP3 has no picture to change.
VIDEO_FILTER_OPERATIONS = frozenset({"crop", "downscale", "upscale"})


class MediaRuleError(ValueError):
    """A request or a source that breaks a media rule. `str()` is user-facing."""


def format_duration(seconds: float) -> str:
    """`754.2` -> `"12:34"`; `3725` -> `"1:02:05"`. Whole seconds, rounded up.

    Rounded up so a video that is 300.4 s long is never described as "5:00"
    in the same sentence that says the limit is 5:00.
    """
    total = max(0, math.ceil(seconds))
    hours, remainder = divmod(total, 3600)
    minutes, secs = divmod(remainder, 60)
    if hours:
        return f"{hours}:{minutes:02d}:{secs:02d}"
    return f"{minutes}:{secs:02d}"


def describe_limit(seconds: int) -> str:
    """The limit as a person would say it: `"5 minutes"`, `"90 seconds"`."""
    if seconds % 60 == 0:
        minutes = seconds // 60
        return f"{minutes} minute" + ("" if minutes == 1 else "s")
    return f"{seconds} seconds"


def check_source_duration(duration_seconds: float, *, max_duration_seconds: int) -> None:
    """Refuse a source longer than the limit, before anything is encoded.

    The limit is by duration, not bytes: encode time -- and so whether a job
    finishes inside the worker's timeout -- follows how long the video is, and
    a byte limit says almost nothing about that once uploads are 4K.
    """
    if max_duration_seconds <= 0:
        return
    if not math.isfinite(duration_seconds) or duration_seconds <= 0:
        raise MediaRuleError("the video's length could not be determined")
    if duration_seconds > max_duration_seconds:
        raise MediaRuleError(
            f"this video is {format_duration(duration_seconds)} long; "
            f"the limit is {describe_limit(max_duration_seconds)}. "
            "Trim it and upload it again"
        )


def editable_height(
    *, height: int | None, is_edit: bool, mp4_max_height: int
) -> int | None:
    """The height of the copy an edit of this job actually starts from.

    Edits run on the job's MP4, not on the original upload. For an upload the
    stored `height` is the original's, and the MP4 is that capped at
    `mp4_max_height` (FFmpeg's `scale=-2:min(cap,ih)`). For an edit job the
    stored height, when there is one, already describes its own output.

    `None` when nothing was stored -- every job from before sprint 4, and an
    edit whose output was never probed. The API then skips its early check and
    leaves the decision to the worker, which probes the real file.
    """
    if height is None or height <= 0:
        return None
    if is_edit:
        return height
    return min(height, mp4_max_height) if mp4_max_height > 0 else height


def scale_options(input_height: int | None) -> dict[str, list[int]] | None:
    """What the editor should offer for a video this tall.

    Downscale: every rung below it, largest first. Upscale: every rung above
    it up to 2160p, smallest first -- so a 2160p video is offered no upscale at
    all, and a 240p one no downscale.
    """
    if input_height is None or input_height <= 0:
        return None
    return {
        "downscale": sorted((h for h in SCALE_HEIGHTS if h < input_height), reverse=True),
        "upscale": [h for h in SCALE_HEIGHTS if h > input_height],
    }


def check_scale(operation: str, target_height: int, input_height: int | None) -> None:
    """One scale request against the height it would be applied to.

    With no `input_height` only the rung itself is checked: that much needs no
    knowledge of the source.
    """
    if target_height not in SCALE_HEIGHTS:
        rungs = ", ".join(f"{h}p" for h in SCALE_HEIGHTS)
        raise MediaRuleError(f"{operation} height must be one of {rungs}")
    if input_height is None:
        return
    if operation == "downscale" and target_height >= input_height:
        raise MediaRuleError(
            f"downscale must be to a height below the video's {input_height}p; "
            f"{target_height}p is not smaller"
        )
    if operation == "upscale" and target_height <= input_height:
        raise MediaRuleError(
            f"upscale must be to a height above the video's {input_height}p; "
            f"{target_height}p is not larger"
        )


def check_edit_operations(
    operations: Iterable[tuple[str, dict]], *, input_height: int | None
) -> None:
    """The source-aware rules for one edit request.

    `operations` is `(name, params)` pairs, so the API can pass its parsed
    request and the worker its stored operations without either depending on
    the other's types. `input_height` is the height of the video the edit
    starts from, or `None` if it is not known yet -- in which case only the
    rules that need no dimensions are applied.

    Scale is checked against the frame it is actually applied to: the worker
    always crops before it scales, so with a crop that is the crop's height,
    not the video's.
    """
    named = {name: params for name, params in operations}

    convert = named.get("convert")
    if convert is not None and convert.get("format") == "mp3":
        if VIDEO_FILTER_OPERATIONS & named.keys():
            raise MediaRuleError("crop and scale cannot be combined with MP3 conversion")

    crop = named.get("crop")
    frame_height = crop.get("h") if crop is not None else input_height
    if type(frame_height) is not int or frame_height <= 0:
        # A malformed crop is the crop validator's to report, with its own
        # message; scale is then checked against the rungs alone.
        frame_height = None
    for operation in ("downscale", "upscale"):
        params = named.get(operation)
        if params is None:
            continue
        target = params.get("height")
        if type(target) is not int:
            raise MediaRuleError(f"{operation} height must be a whole number of pixels")
        check_scale(operation, target, frame_height)
