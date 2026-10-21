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
from collections.abc import Callable, Iterable
from dataclasses import dataclass

# The heights an edit may scale to. Fixed rungs rather than any number: an odd
# height is one libx264 refuses mid-encode, and a free-form number is not
# something the editor's dropdowns can offer anyway. 2160p is the top because
# it is the top of the ladder -- an upscale past it would be stored and then
# never played at its own size.
SCALE_HEIGHTS: tuple[int, ...] = (240, 360, 480, 720, 1080, 1440, 2160)
MAX_SCALE_HEIGHT = SCALE_HEIGHTS[-1]

# The order the worker applies operations in, whatever order a request lists
# them (sprint 6 plan, 2.1). Crop runs before rotation, so its rectangle stays
# in the original's pixels; colour and presets run after the HDR tone-map, so
# they always see standard 8-bit video. A new operation takes its place here.
OPERATION_ORDER: tuple[str, ...] = (
    "clip",
    "crop",
    "rotate",
    "flip",
    "downscale",
    "upscale",
    "adjust",
    "preset",
    "speed",
    "convert",
)


class MediaRuleError(ValueError):
    """A request or a source that breaks a media rule. `str()` is user-facing."""


@dataclass(frozen=True, slots=True)
class Frame:
    """The picture's size at one point in the pipeline. Either side may be
    unknown (None): the API knows only what was stored, and an operation it
    cannot size -- a malformed crop -- leaves the rest unchecked."""

    width: int | None
    height: int | None


@dataclass(frozen=True, slots=True)
class OperationRule:
    """One operation's rule, shared by the API and the worker.

    `check(params, frame)` raises `MediaRuleError` for a request this frame
    cannot take, and returns the frame the operation leaves behind -- so a
    scale after a 90-degree rotation is judged against the rotated picture.
    `label` names it in messages; `changes_picture` is what an MP3 cannot take.
    """

    label: str
    check: Callable[[dict, Frame], Frame]
    changes_picture: bool = True


OPERATION_RULES: dict[str, OperationRule] = {}


def register_rule(name: str, rule: OperationRule) -> OperationRule:
    """Add an operation's rule. One line per operation, at the bottom of this
    module; the name must already be in `OPERATION_ORDER`."""
    if name not in OPERATION_ORDER:
        raise ValueError(f"{name!r} has no place in OPERATION_ORDER")
    if name in OPERATION_RULES:
        raise ValueError(f"{name!r} is registered twice")
    OPERATION_RULES[name] = rule
    return rule


def ordered(names: Iterable[str]) -> list[str]:
    """`names` in the order the worker applies them."""
    wanted = set(names)
    return [name for name in OPERATION_ORDER if name in wanted]


def _join_labels(labels: list[str]) -> str:
    if len(labels) == 1:
        return labels[0]
    return ", ".join(labels[:-1]) + " and " + labels[-1]


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
    operations: Iterable[tuple[str, dict]],
    *,
    input_height: int | None,
    input_width: int | None = None,
) -> None:
    """The source-aware rules for one edit request.

    `operations` is `(name, params)` pairs, so the API can pass its parsed
    request and the worker its stored operations without either depending on
    the other's types. `input_height` (and `input_width`, when known) is the
    size of the video the edit starts from, or `None` if it is not known yet --
    in which case only the rules that need no dimensions are applied.

    Each operation is checked against the frame the ones before it leave, in
    `OPERATION_ORDER`: a scale after a crop is judged against the crop, not
    the whole video.
    """
    named = {name: params for name, params in operations}

    convert = named.get("convert")
    if convert is not None and convert.get("format") == "mp3":
        labels: list[str] = []
        for name in ordered(named):
            rule = OPERATION_RULES.get(name)
            if rule is not None and rule.changes_picture and rule.label not in labels:
                labels.append(rule.label)
        if labels:
            raise MediaRuleError(f"{_join_labels(labels)} cannot be combined with MP3 conversion")

    frame = Frame(width=input_width, height=input_height)
    for name in ordered(named):
        rule = OPERATION_RULES.get(name)
        if rule is not None:
            frame = rule.check(named[name], frame)


# --- the rules ------------------------------------------------------------
#
# A new operation adds a function here and one `register_rule` line below.
# Its parameters' shape is the schema's to enforce (app/schemas/edit.py);
# these are the rules that need the video's size.


def _known(value) -> int | None:
    return value if type(value) is int and value > 0 else None


def _crop_rule(params: dict, frame: Frame) -> Frame:
    # Whether the box fits is the worker's to say, against the real file
    # (app.worker.validation.validate_crop). A malformed crop is reported
    # there too, so the frame after it is simply unknown.
    return Frame(width=_known(params.get("w")), height=_known(params.get("h")))


def _scale_rule(operation: str) -> Callable[[dict, Frame], Frame]:
    def check(params: dict, frame: Frame) -> Frame:
        target = params.get("height")
        if type(target) is not int:
            raise MediaRuleError(f"{operation} height must be a whole number of pixels")
        check_scale(operation, target, frame.height)
        width = None
        if frame.width and frame.height:
            # scale=-2:H keeps the shape and rounds the width to even.
            width = max(2, round(frame.width * target / frame.height / 2) * 2)
        return Frame(width=width, height=target)

    return check


def _unchanged(_params: dict, frame: Frame) -> Frame:
    return frame


def _rotate_rule(params: dict, frame: Frame) -> Frame:
    if params["degrees"] in (90, 270):
        return Frame(width=frame.height, height=frame.width)
    return frame


register_rule("rotate", OperationRule(label="rotate", check=_rotate_rule))
register_rule("flip", OperationRule(label="flip", check=_unchanged))
register_rule("speed", OperationRule(label="speed", check=_unchanged, changes_picture=False))
register_rule("clip", OperationRule(label="clip", check=_unchanged, changes_picture=False))
register_rule("crop", OperationRule(label="crop", check=_crop_rule))
register_rule("downscale", OperationRule(label="scale", check=_scale_rule("downscale")))
register_rule("upscale", OperationRule(label="scale", check=_scale_rule("upscale")))
register_rule("convert", OperationRule(label="convert", check=_unchanged, changes_picture=False))
