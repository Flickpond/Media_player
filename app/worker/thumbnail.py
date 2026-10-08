"""One poster frame per video, taken right after the probe.

The Library shows a card per video, and a card without a picture is a row of
filenames. One JPEG per job is enough for that: a single frame, decoded with
a fast input-side seek and scaled down into a small box, costs well under a
second even on a 4K source -- next to nothing beside the encode it precedes.

Best-effort by design. A thumbnail that cannot be made is a missing picture,
never a failed upload: every failure here raises `ThumbnailError`, which the
processor logs and moves past.
"""

import subprocess
from pathlib import Path

from app.worker.probe import SourceProbe

THUMBNAIL_CONTENT_TYPE = "image/jpeg"
THUMBNAIL_FILENAME = "thumbnail.jpg"

# How far in to take the frame. The first frame is the worst choice -- fades
# from black and phone camera start-up are both there -- and a second in
# clears both for nearly every clip.
PREFERRED_OFFSET_SECONDS = 1.0


class ThumbnailError(RuntimeError):
    """The frame could not be taken. Diagnostic only; nobody is shown this."""


def thumbnail_offset(duration_seconds: float) -> float:
    """A second in, or the middle of a clip too short to have one."""
    if duration_seconds <= 0:
        return 0.0
    return min(PREFERRED_OFFSET_SECONDS, duration_seconds / 2)


def build_thumbnail_command(
    *,
    source_path: Path,
    output_path: Path,
    offset_seconds: float,
    max_width: int,
    max_height: int,
    ffmpeg_binary: str = "ffmpeg",
) -> list[str]:
    # `-ss` before `-i` seeks the input to the nearest keyframe instead of
    # decoding everything up to the offset, which is what keeps this fast on
    # a long 4K file. The box is a bound, not a target: a portrait video
    # comes out portrait, and nothing smaller than the box is enlarged.
    scale = (
        f"scale=w='min({max_width},iw)':h='min({max_height},ih)'"
        ":force_original_aspect_ratio=decrease"
    )
    return [
        ffmpeg_binary,
        "-hide_banner",
        "-loglevel",
        "error",
        "-ss",
        f"{offset_seconds:.3f}",
        "-i",
        str(source_path),
        "-frames:v",
        "1",
        "-an",
        "-vf",
        scale,
        "-q:v",
        "4",
        "-y",
        str(output_path),
    ]


def extract_thumbnail(
    source_path: Path,
    output_path: Path,
    probe: SourceProbe,
    *,
    ffmpeg_binary: str = "ffmpeg",
    max_width: int = 640,
    max_height: int = 360,
    timeout_seconds: int = 30,
    runner=subprocess.run,
) -> Path:
    """Write one JPEG frame of `source_path` to `output_path`."""
    command = build_thumbnail_command(
        source_path=source_path,
        output_path=output_path,
        offset_seconds=thumbnail_offset(probe.duration_seconds),
        max_width=max_width,
        max_height=max_height,
        ffmpeg_binary=ffmpeg_binary,
    )
    try:
        result = runner(
            command,
            capture_output=True,
            text=True,
            timeout=timeout_seconds,
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        raise ThumbnailError(f"thumbnail timed out after {timeout_seconds}s") from exc
    if result.returncode != 0:
        detail = (result.stderr or "").strip()[-500:] or "no FFmpeg error output"
        raise ThumbnailError(f"thumbnail extraction failed: {detail}")
    # FFmpeg exits 0 without writing anything when the seek lands past the
    # last decodable frame, so success is the file, not the exit code.
    if not output_path.is_file() or output_path.stat().st_size == 0:
        raise ThumbnailError("FFmpeg produced no thumbnail frame")
    return output_path
