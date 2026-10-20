"""The seek-bar preview: a sheet of small frames and its WebVTT index.

Sprint 6 plan, 2.2. Hovering the seek bar shows the frame at that moment;
Plyr's `previewThumbnails` does the showing, from a WebVTT file whose cues
each name a rectangle of one JPEG. So a video gets two objects beside its
MP4, under `<output dir>/preview/`:

* `sprite.jpg` -- one frame every `interval` seconds, `tile_width` px wide,
  laid out left to right, top to bottom;
* `sprite.vtt` -- one cue per frame, `sprite.jpg#xywh=x,y,w,h`.

The cues name the sheet relatively, not by URL: objects are private, and a
signed URL written into a stored file would expire with its signature. The
API serves the index with the name swapped for a freshly signed one
(app/api/preview.py), and only once the job's row records it (`preview_key`).

Taken from the finished MP4 rather than the original: it is already standard
range and the right way up, and at most 1080p, so a 4K HDR upload costs no
more to preview than any other. Runs as an `after_ladder` hook, so it can
only ever add to a video: see that module for why a failure here is logged
and nothing more.
"""

import asyncio
import logging
import math
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from uuid import UUID

from app.config import get_settings
from app.repositories.jobs import mark_preview_ready
from app.worker.db import get_worker_session_factory
from app.worker.interruption import run as run_process

logger = logging.getLogger("app.worker.preview")

PREVIEW_DIRNAME = "preview"
SPRITE_FILENAME = "sprite.jpg"
VTT_FILENAME = "sprite.vtt"
SPRITE_CONTENT_TYPE = "image/jpeg"
VTT_CONTENT_TYPE = "text/vtt"

COLUMNS = 10
# The player downloads the whole sheet on the first hover. Past this many
# frames the interval widens instead, so an hour-long edit still gets one
# sheet of a few hundred kilobytes rather than 720 frames.
MAX_FRAMES = 200


class PreviewError(RuntimeError):
    """The sheet could not be made. Diagnostic only; nobody is shown this."""


@dataclass(frozen=True, slots=True)
class SpriteLayout:
    interval_seconds: int
    frames: int
    columns: int
    rows: int
    tile_width: int
    tile_height: int


def preview_prefix_for(output_key: str) -> str:
    """`outputs/<job>/demo.mp4` -> `outputs/<job>/preview/`."""
    return f"{PurePosixPath(output_key).parent}/{PREVIEW_DIRNAME}/"


def plan_sprite(
    *,
    width: int,
    height: int,
    duration_seconds: float,
    interval_seconds: int,
    tile_width: int,
    columns: int = COLUMNS,
    max_frames: int = MAX_FRAMES,
) -> SpriteLayout:
    if width <= 0 or height <= 0 or duration_seconds <= 0:
        raise PreviewError(f"nothing to preview: {width}x{height}, {duration_seconds}s")
    if interval_seconds <= 0 or tile_width <= 0:
        raise PreviewError(f"bad preview settings: every {interval_seconds}s, {tile_width}px")
    # Whole seconds, so the cue times and FFmpeg's `fps=1/N` agree exactly.
    interval = max(interval_seconds, math.ceil(duration_seconds / max_frames))
    frames = math.ceil(duration_seconds / interval)
    columns = min(columns, frames)
    # Fixed here rather than left to FFmpeg's `-2`, because the index has to
    # state each rectangle exactly; even, because yuvj420p needs it.
    tile_height = max(2, round(tile_width * height / width / 2) * 2)
    return SpriteLayout(
        interval_seconds=interval,
        frames=frames,
        columns=columns,
        rows=math.ceil(frames / columns),
        tile_width=tile_width,
        tile_height=tile_height,
    )


def build_sprite_command(
    *, source_path: Path, output_path: Path, layout: SpriteLayout, ffmpeg_binary: str = "ffmpeg"
) -> list[str]:
    # `fps` before `scale`: only the frames kept are scaled. `tile` writes the
    # sheet once it is full or the input ends, so a sheet is always written
    # even when FFmpeg rounds the last frame away; that cell is left black.
    filters = (
        f"fps=1/{layout.interval_seconds},"
        f"scale={layout.tile_width}:{layout.tile_height},setsar=1,"
        f"tile={layout.columns}x{layout.rows}"
    )
    return [
        ffmpeg_binary,
        "-hide_banner",
        "-loglevel",
        "error",
        "-i",
        str(source_path),
        "-an",
        "-sn",
        "-dn",
        "-vf",
        filters,
        "-frames:v",
        "1",
        "-q:v",
        "5",
        "-y",
        str(output_path),
    ]


def _timestamp(seconds: float) -> str:
    millis = round(seconds * 1000)
    hours, millis = divmod(millis, 3_600_000)
    minutes, millis = divmod(millis, 60_000)
    secs, millis = divmod(millis, 1000)
    return f"{hours:02d}:{minutes:02d}:{secs:02d}.{millis:03d}"


def build_vtt(
    layout: SpriteLayout, duration_seconds: float, sprite_name: str = SPRITE_FILENAME
) -> str:
    lines = ["WEBVTT", ""]
    for index in range(layout.frames):
        start = index * layout.interval_seconds
        end = min(start + layout.interval_seconds, duration_seconds)
        row, column = divmod(index, layout.columns)
        x, y = column * layout.tile_width, row * layout.tile_height
        lines += [
            f"{_timestamp(start)} --> {_timestamp(end)}",
            f"{sprite_name}#xywh={x},{y},{layout.tile_width},{layout.tile_height}",
            "",
        ]
    return "\n".join(lines)


class PreviewSprite:
    """Makes, stores and records one video's sheet and index. Raises on any
    failure, leaving nothing behind that the row does not point at."""

    def __init__(
        self,
        store,
        *,
        prober,
        record,
        ffmpeg_binary: str = "ffmpeg",
        interval_seconds: int = 5,
        tile_width: int = 160,
        timeout_seconds: int = 180,
        runner=run_process,
    ) -> None:
        self._store = store
        self._prober = prober
        # `record(job_id, preview_key) -> bool`: False when the job no longer
        # has a ladder to preview (record_preview below).
        self._record = record
        self._ffmpeg_binary = ffmpeg_binary
        self._interval_seconds = interval_seconds
        self._tile_width = tile_width
        self._timeout_seconds = timeout_seconds
        self._runner = runner

    def run(self, job_id: UUID, output_key: str) -> str | None:
        """Store and record the sheet and its index; returns the index's key,
        or None if the video was deleted while they were being made."""
        prefix = preview_prefix_for(output_key)
        vtt_key = prefix + VTT_FILENAME
        with tempfile.TemporaryDirectory(prefix=f"preview-{job_id}-") as temp:
            temp_dir = Path(temp)
            source = temp_dir / "source.mp4"
            sprite = temp_dir / SPRITE_FILENAME
            vtt = temp_dir / VTT_FILENAME
            self._store.download_file(key=output_key, destination=str(source))
            probe = self._prober(source)
            layout = plan_sprite(
                width=probe.width,
                height=probe.height,
                duration_seconds=probe.duration_seconds,
                interval_seconds=self._interval_seconds,
                tile_width=self._tile_width,
            )
            self._render(source, sprite, layout)
            vtt.write_bytes(build_vtt(layout, probe.duration_seconds).encode("utf-8"))

            # The index after the sheet, and the row after both: once the API
            # can see a preview, everything it points at is there.
            try:
                self._store.upload_file(
                    key=prefix + SPRITE_FILENAME,
                    source=str(sprite),
                    content_type=SPRITE_CONTENT_TYPE,
                )
                self._store.upload_file(key=vtt_key, source=str(vtt), content_type=VTT_CONTENT_TYPE)
                recorded = self._record(job_id, vtt_key)
            except Exception:
                self._discard(job_id, prefix)
                raise
        if not recorded:
            # Deleted while this ran: the delete found no preview on the row,
            # so nobody else will ever remove these.
            logger.info("job %s: video gone before its preview was recorded; discarding", job_id)
            self._discard(job_id, prefix)
            return None
        logger.info(
            "job %s: seek-bar preview ready (%d frames, every %ds)",
            job_id,
            layout.frames,
            layout.interval_seconds,
        )
        return vtt_key

    def _discard(self, job_id: UUID, prefix: str) -> None:
        try:
            self._store.delete_prefix(prefix)
        except Exception:
            logger.warning("job %s: could not remove an unrecorded preview", job_id)

    def _render(self, source: Path, sprite: Path, layout: SpriteLayout) -> None:
        command = build_sprite_command(
            source_path=source,
            output_path=sprite,
            layout=layout,
            ffmpeg_binary=self._ffmpeg_binary,
        )
        try:
            result = self._runner(
                command,
                capture_output=True,
                text=True,
                timeout=self._timeout_seconds,
                check=False,
            )
        except subprocess.TimeoutExpired as exc:
            raise PreviewError(f"preview timed out after {self._timeout_seconds}s") from exc
        if result.returncode != 0:
            detail = (result.stderr or "").strip()[-500:] or "no FFmpeg error output"
            raise PreviewError(f"preview sheet failed: {detail}")
        if not sprite.is_file() or sprite.stat().st_size == 0:
            raise PreviewError("FFmpeg produced no preview sheet")


def record_preview(job_id: UUID, preview_key: str) -> bool:
    """Write `preview_key` to the job's row. Called from the ladder job's
    worker thread, which has no event loop of its own; the worker's engine
    opens a fresh connection per use (app/worker/db.py), so a loop here is safe."""

    async def record() -> bool:
        async with get_worker_session_factory()() as session:
            return await mark_preview_ready(session, job_id, preview_key=preview_key) is not None

    return asyncio.run(record())


def get_preview_sprite() -> PreviewSprite:
    from functools import partial

    from app.services.minio_client import bucket, internal_client
    from app.worker.probe import probe_source
    from app.worker.storage import MinioObjectStore

    settings = get_settings()
    return PreviewSprite(
        MinioObjectStore(internal_client(), bucket=bucket()),
        prober=partial(
            probe_source,
            ffprobe_binary=settings.worker_ffprobe_binary,
            timeout_seconds=settings.worker_ffprobe_timeout_seconds,
        ),
        record=record_preview,
        ffmpeg_binary=settings.worker_ffmpeg_binary,
        interval_seconds=settings.worker_preview_interval_seconds,
        tile_width=settings.worker_preview_tile_width,
        timeout_seconds=settings.worker_preview_timeout_seconds,
    )


def make_preview(job_id: UUID, output_key: str) -> None:
    """The after-ladder hook, registered by the worker when
    `WORKER_PREVIEW_SPRITE` is on (app/worker/__main__.py)."""
    get_preview_sprite().run(job_id, output_key)
