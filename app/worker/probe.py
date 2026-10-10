"""What the source file actually is: its real dimensions and duration.

Two things this sprint need it, for the same reason. Crop and clip are
validated against the file rather than against what the client claims -- a
crop rectangle is only meaningful in the source's own coordinate space, and
a clip range past the end of the video is a request FFmpeg would accept and
then produce something surprising from. The HLS ladder needs it too, so a
360p upload is never given a 720p rendition it has no detail for.

Every failure here raises `ObjectStoreError`, which is not a naming accident:
`app.worker.tasks.readable_error` only unwraps a user-facing message from
that type, and everything else becomes the generic "processing failed
unexpectedly". A probe failure means the file is unreadable, which the
uploader can act on, so it has to arrive as the type that carries a message.
"""

import json
import subprocess
from dataclasses import dataclass
from pathlib import Path

from app.worker.storage import UNPROCESSABLE_VIDEO, ObjectStoreError

# The one probe refusal the uploader can fix by choosing a different file,
# rather than a different copy of the same one: an audio file, or a container
# whose only stream is not video.
NO_VIDEO_STREAM = "this file has no video track; please upload a video file"


# Stream size and the container's duration, plus the two places a rotation can
# be recorded: display-matrix side data (current FFmpeg) and the legacy
# `rotate` tag (files written by older muxers).
PROBE_ENTRIES = "stream=width,height:stream_side_data=rotation:stream_tags=rotate:format=duration"


@dataclass(frozen=True, slots=True)
class SourceProbe:
    width: int
    height: int
    duration_seconds: float


def probe_source(
    path: Path,
    *,
    ffprobe_binary: str = "ffprobe",
    timeout_seconds: int = 30,
    runner=subprocess.run,
) -> SourceProbe:
    """Read the first video stream's size and the container's duration.

    `runner` is injectable for the same reason `FfmpegProcessor` takes one:
    the interesting cases here are what happens when the probe fails, and
    those are not worth a real corrupt file per test.
    """
    command = [
        ffprobe_binary,
        "-v",
        "error",
        "-select_streams",
        "v:0",
        # One -show_entries with a colon-separated spec: the form ffprobe's
        # own documentation gives. Two separate flags also accumulate on the
        # image's current ffprobe (7.1.5, Debian 13) -- checked, rather than
        # assumed -- but that is a behaviour nothing here pins, and the half
        # that would go missing if it ever changed comes back as an absent
        # key, not an error. The documented form costs nothing.
        #
        # The rotation entries are what make a phone video come out the right
        # way up: see `_display_size`.
        "-show_entries",
        PROBE_ENTRIES,
        "-of",
        "json",
        str(path),
    ]

    try:
        result = runner(
            command,
            capture_output=True,
            text=True,
            timeout=timeout_seconds,
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        raise ObjectStoreError(
            f"ffprobe timed out after {timeout_seconds}s on {path}",
            user_message=UNPROCESSABLE_VIDEO,
        ) from exc

    if result.returncode != 0:
        detail = (result.stderr or "").strip()[:500] or "no ffprobe error output"
        raise ObjectStoreError(f"ffprobe failed: {detail}", user_message=UNPROCESSABLE_VIDEO)

    try:
        payload = json.loads(result.stdout or "")
    except json.JSONDecodeError as exc:
        raise ObjectStoreError(
            f"ffprobe returned output that is not JSON: {(result.stdout or '')[:200]!r}",
            user_message=UNPROCESSABLE_VIDEO,
        ) from exc

    streams = payload.get("streams") or []
    if not streams:
        # An audio-only container, or one whose video stream ffprobe could
        # not parse. Uploads are content-sniffed as video, so this is rare
        # rather than impossible -- sniffing proves the container, not that
        # every stream inside it is intact.
        raise ObjectStoreError(
            f"ffprobe found no video stream in {path}",
            user_message=NO_VIDEO_STREAM,
        )

    try:
        width, height = _display_size(streams[0])
        duration = float(payload["format"]["duration"])
    except (KeyError, TypeError, ValueError) as exc:
        # Some containers genuinely carry no duration, and a stream can
        # report no dimensions. Both are "we cannot validate against this
        # file", which is a refusal, not something to guess a default for.
        raise ObjectStoreError(
            f"ffprobe output missing width, height or duration: {payload!r:.200}",
            user_message=UNPROCESSABLE_VIDEO,
        ) from exc

    if width <= 0 or height <= 0 or duration <= 0:
        raise ObjectStoreError(
            f"ffprobe reported a non-positive dimension or duration: "
            f"{width}x{height}, {duration}s",
            user_message=UNPROCESSABLE_VIDEO,
        )

    return SourceProbe(width=width, height=height, duration_seconds=duration)


def _rotation_degrees(stream: dict) -> int:
    """The clockwise rotation a player applies, from whichever place has it."""
    for side_data in stream.get("side_data_list") or []:
        if isinstance(side_data, dict) and "rotation" in side_data:
            try:
                return int(float(side_data["rotation"]))
            except (TypeError, ValueError):
                return 0
    try:
        return int(float((stream.get("tags") or {}).get("rotate", 0)))
    except (TypeError, ValueError):
        return 0


def _display_size(stream: dict) -> tuple[int, int]:
    """Width and height as the video is *shown*, not as it is stored.

    A phone held upright records landscape frames and a rotation flag. ffprobe
    reports the stored frame -- 3840x2160 for an upright 4K phone video -- but
    FFmpeg applies the flag before any filter runs, so every encode, the
    ladder, the thumbnail and the crop box all see 2160x3840. A height read
    from the stored frame would cap that video's ladder at 2160 when its frames
    are 3840 tall, and size its edit options from the wrong side.
    """
    width = int(stream["width"])
    height = int(stream["height"])
    if _rotation_degrees(stream) % 180 != 0:
        return height, width
    return width, height
