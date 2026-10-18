"""Worker storage seam and processing implementations.

The worker downloads source objects, processes them, and uploads outputs through
this module. Keeping that seam separate from the job state machine lets Sprint 2
replace the copy stand-in with FFmpeg without changing task orchestration.
"""

import logging
import shutil
import subprocess
import tempfile
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path, PurePosixPath
from typing import Protocol
from uuid import UUID

from minio import Minio
from minio.commonconfig import CopySource

from app.config import get_settings
from app.services.minio_client import bucket, internal_client
from app.worker.interruption import run as run_process

# One message for every "the file itself is the problem" case. FFmpeg's own
# stderr names the temp path it was working on and assumes the reader knows what
# a codec is; neither belongs on the screen of someone who uploaded a holiday
# video.
UNPROCESSABLE_VIDEO = (
    "the video could not be processed; it may be corrupt or in a format we cannot read"
)

logger = logging.getLogger("app.worker.storage")

# Encode work grows with frames as well as pixels, and a phone's 60 fps costs
# twice its 30 fps -- so 30 unless the uploader chose to keep 60 (Job.max_fps).
DEFAULT_MAX_FPS = 30


# HDR to standard range: linearise, map primaries to BT.709, compress the
# highlights with Hable's curve (no desaturation, so colours keep their
# depth), then back to BT.709's gamma in TV range. The float RGB step is
# what lets the curve work on real light levels rather than coded values.
HDR_TO_SDR = (
    "zscale=t=linear:npl=100,format=gbrpf32le,zscale=p=bt709,"
    "tonemap=tonemap=hable:desat=0,zscale=t=bt709:m=bt709:r=tv"
)

# What every browser plays: 8-bit 4:2:0. Without it a 10-bit or 4:2:2 source
# makes libx264 write a profile (High 10, High 4:2:2) nothing streams.
SDR_PIXELS = "format=yuv420p"

# Tags for a tone-mapped output, so no player mistakes it for HDR.
BT709_TAGS = ["-colorspace", "bt709", "-color_primaries", "bt709", "-color_trc", "bt709"]


def sdr_filters(hdr: bool) -> list[str]:
    """The tail of every video filter chain: tone-map if HDR, then 8-bit 4:2:0."""
    return [HDR_TO_SDR, SDR_PIXELS] if hdr else [SDR_PIXELS]


def mp4_video_filters(max_fps: int, max_height: int, hdr: bool) -> list[str]:
    """The MP4's video chain: cap the rate, cap the height, then 8-bit SDR.

    Tone-mapped after the downscale: the same picture, at a quarter of the
    pixels a 4K source would cost. One definition for the single encode and
    every piece of a chunked one (app.worker.chunked), so they cannot drift.
    """
    return [fps_cap_filter(max_fps), f"scale=-2:min({max_height}\\,ih)", *sdr_filters(hdr)]


def fps_cap_filter(max_fps: int) -> str:
    """An FFmpeg filter that lowers the frame rate to `max_fps`, never raises it.

    `min` against the source's own rate, so a 24 or 25 fps film is left alone
    rather than padded with duplicated frames. First in every chain, so
    frames that would be dropped are never scaled first.
    """
    return f"fps=fps=min({max_fps}\\,source_fps)"


@dataclass(frozen=True, slots=True)
class ProcessingResult:
    """What a processing step produced.

    `hls_key` is optional because the adaptive ladder is best-effort: a job
    whose ladder failed is still `done` and still playable through
    `output_key`. A step that does not build ladders at all simply leaves it
    `None`, which is why this is a dataclass with a default rather than a
    tuple every caller has to unpack in the right order.

    `ladder_pending` says a ladder should be built afterwards, as its own
    queued job, so the MP4 reaches the uploader without waiting for it.
    """

    output_key: str
    hls_key: str | None = None
    ladder_pending: bool = False
    # What the source actually is, when it could be probed. Stored on the job
    # by `mark_done`; the step itself never touches the database.
    width: int | None = None
    height: int | None = None
    duration_seconds: float | None = None
    # The poster frame, when one could be taken. Best-effort, like the ladder:
    # a job without one is still done and still playable.
    thumbnail_key: str | None = None


class ObjectStoreError(RuntimeError):
    """A storage or processing failure, carrying two messages deliberately.

    `str(exc)` is the **diagnostic**: object keys, S3 codes, FFmpeg stderr. It
    goes to the log, where an operator can act on it.

    `user_message` is what reaches the job's `error` column, and from there
    `GET /jobs/{id}` and the uploader's screen. It names a cause they can do
    something about without naming anything internal -- not a key, not a temp
    path, not an exception class.

    Both are required. This class used to carry one message that served both
    audiences, which is how object keys and raw FFmpeg output ended up in a
    user-facing column (P9). Making the split a TypeError means the next raise
    site that forgets fails in CI rather than in production.
    """

    def __init__(self, diagnostic: str, *, user_message: str) -> None:
        super().__init__(diagnostic)
        self.user_message = user_message


class ObjectStore(Protocol):
    def object_exists(self, key: str) -> bool: ...

    def copy_object(self, *, source_key: str, output_key: str) -> None: ...

    def download_file(self, *, key: str, destination: str) -> None: ...

    def upload_file(self, *, key: str, source: str, content_type: str = ...) -> None: ...

    def delete_prefix(self, prefix: str) -> None: ...


class MinioObjectStore:
    def __init__(self, client: Minio, *, bucket: str) -> None:
        self._client = client
        self._bucket = bucket

    def object_exists(self, key: str) -> bool:
        from minio.error import S3Error

        try:
            self._client.stat_object(self._bucket, key)
        except S3Error as exc:
            if exc.code in {"NoSuchKey", "NoSuchObject", "NotFound"}:
                return False
            raise ObjectStoreError(
                f"object store error checking {key}: {exc.code}",
                user_message="storage could not be reached; please try again",
            ) from exc
        return True

    def copy_object(self, *, source_key: str, output_key: str) -> None:
        from minio.error import S3Error

        try:
            self._client.copy_object(
                self._bucket,
                output_key,
                CopySource(self._bucket, source_key),
            )
        except S3Error as exc:
            raise ObjectStoreError(
                f"copy failed from {source_key} to {output_key}: {exc.code}",
                user_message="the processed video could not be saved; please try again",
            ) from exc

    def download_file(self, *, key: str, destination: str) -> None:
        from minio.error import S3Error

        try:
            self._client.fget_object(self._bucket, key, destination)
        except S3Error as exc:
            raise ObjectStoreError(
                f"download failed for {key}: {exc.code}",
                user_message="the uploaded file could not be read back; please try again",
            ) from exc

    def upload_file(self, *, key: str, source: str, content_type: str = "video/mp4") -> None:
        # The default keeps every existing caller writing MP4s unchanged. It
        # is a parameter because the HLS ladder writes playlists and segments
        # through this same method, and a manifest served as `video/mp4` is
        # the kind of mistake that looks fine in storage and only surfaces as
        # a player refusing to load it.
        from minio.error import S3Error

        try:
            self._client.fput_object(self._bucket, key, source, content_type=content_type)
        except S3Error as exc:
            raise ObjectStoreError(
                f"upload failed for {key}: {exc.code}",
                user_message="the processed video could not be saved; please try again",
            ) from exc


    def delete_prefix(self, prefix: str) -> None:
        from minio.error import S3Error

        try:
            for item in self._client.list_objects(self._bucket, prefix=prefix, recursive=True):
                self._client.remove_object(self._bucket, item.object_name)
        except S3Error as exc:
            raise ObjectStoreError(
                f"cleanup failed under {prefix}: {exc.code}",
                user_message="storage could not be reached; please try again",
            ) from exc


class ProcessingStep(Protocol):
    def run(
        self, *, job_id: UUID, source_key: str, max_fps: int = DEFAULT_MAX_FPS
    ) -> ProcessingResult:
        """Process the source object and return what it produced."""


class FfmpegProcessor:
    """Download a source object, transcode it to MP4, and upload the result."""

    def __init__(
        self,
        store: ObjectStore,
        *,
        output_prefix: str,
        ffmpeg_binary: str = "ffmpeg",
        preset: str = "veryfast",
        crf: int = 23,
        max_height: int = 1080,
        timeout_seconds: int = 870,
        runner=run_process,
        builds_ladder: bool = False,
        prober=None,
        source_check=None,
        thumbnailer=None,
        chunked=None,
        progress=None,
    ) -> None:
        self._store = store
        self._output_prefix = output_prefix.strip("/")
        self._ffmpeg_binary = ffmpeg_binary
        self._preset = preset
        self._crf = crf
        self._max_height = max_height
        self._timeout_seconds = timeout_seconds
        self._runner = runner
        # The prober is injected rather than imported: `app.worker.probe`
        # imports this module, so importing it back at module level would be
        # a cycle. Injection also means the MP4 path is testable without it.
        # The ladder itself is no longer built here -- it is its own job (see
        # `app.worker.tasks.build_ladder_async`); this only says one is due.
        self._builds_ladder = builds_ladder
        self._prober = prober
        # Track C's probe-first rejection: `source_check(probe)` raises a
        # ValueError whose message is written for the uploader. Supplying it
        # also makes the probe mandatory -- a source that cannot be probed is
        # refused in seconds instead of being encoded blind.
        self._source_check = source_check
        # Track C's poster frame: `thumbnailer(source_path, output_path, probe)`
        # writes a JPEG. Best-effort; see `_take_thumbnail`.
        self._thumbnailer = thumbnailer
        # app.worker.chunked.ChunkedMp4, when WORKER_CHUNKED_MP4 is on: a long
        # source is encoded in pieces across workers instead of in one run.
        self._chunked = chunked
        # `progress(job_id, percent)` (app.services.progress.set_progress):
        # how far the MP4 has got, for the page. Best-effort.
        self._progress = progress

    def output_key_for(self, *, job_id: UUID, source_key: str) -> str:
        stem = PurePosixPath(source_key).stem or str(job_id)
        return f"{self._output_prefix}/{job_id}/{stem}.mp4"

    def thumbnail_key_for(self, *, job_id: UUID) -> str:
        from app.worker.thumbnail import THUMBNAIL_FILENAME

        return f"{self._output_prefix}/{job_id}/{THUMBNAIL_FILENAME}"

    def _probe(self, *, job_id: UUID, source_path: Path):
        """What the source is, or None if it couldn't be read.

        Runs first, before any encoding, so the result can be stored on the job
        and reused by the ladder instead of probing twice.

        Without a `source_check` a failure is logged, not raised, and costs only
        the dimensions and the ladder. With one, this is the probe-first
        rejection: a source that is corrupt, has no video stream or breaks a
        media rule fails the job here, before a second is spent encoding it.
        """
        if self._prober is None:
            return None
        if self._source_check is None:
            try:
                return self._prober(source_path)
            except Exception:
                logger.exception("job %s: could not probe the source", job_id)
                return None

        # The probe's own ObjectStoreError already carries a message for the
        # uploader ("corrupt", "no video track"), so it propagates unchanged.
        probe = self._prober(source_path)
        try:
            self._source_check(probe)
        except ValueError as exc:
            raise ObjectStoreError(
                f"source refused by media rules: {exc}", user_message=str(exc)
            ) from exc
        return probe

    def _take_thumbnail(self, *, job_id: UUID, source_path: Path, probe, temp_dir: Path):
        """The poster frame's local path, or None. Never fails the job."""
        if self._thumbnailer is None or probe is None:
            return None
        try:
            return self._thumbnailer(source_path, temp_dir / "thumbnail.jpg", probe)
        except Exception:
            logger.warning("job %s: no thumbnail", job_id, exc_info=True)
            return None

    def _store_thumbnail(self, *, job_id: UUID, thumbnail_path) -> str | None:
        """Upload the poster frame. Its key, or None if it could not be stored."""
        if thumbnail_path is None:
            return None
        from app.worker.thumbnail import THUMBNAIL_CONTENT_TYPE

        key = self.thumbnail_key_for(job_id=job_id)
        try:
            self._store.upload_file(
                key=key, source=str(thumbnail_path), content_type=THUMBNAIL_CONTENT_TYPE
            )
        except Exception:
            logger.warning("job %s: thumbnail could not be stored", job_id, exc_info=True)
            return None
        return key

    def run(
        self, *, job_id: UUID, source_key: str, max_fps: int = DEFAULT_MAX_FPS
    ) -> ProcessingResult:
        if not self._store.object_exists(source_key):
            raise ObjectStoreError(
                f"source object missing from storage: {source_key}",
                user_message="the uploaded file is no longer in storage; please upload it again",
            )

        output_key = self.output_key_for(job_id=job_id, source_key=source_key)
        temp_dir = Path(tempfile.mkdtemp(prefix="flickpond-transcode-"))
        source_path = temp_dir / "source"
        output_path = temp_dir / "output.mp4"
        try:
            self._store.download_file(key=source_key, destination=str(source_path))
            probe = self._probe(job_id=job_id, source_path=source_path)
            # Taken now, right after the probe and before the long encode, but
            # only uploaded once the MP4 is: a job that fails mid-encode then
            # leaves no picture behind in storage that no row points at.
            thumbnail_path = self._take_thumbnail(
                job_id=job_id, source_path=source_path, probe=probe, temp_dir=temp_dir
            )
            hdr = probe.hdr if probe is not None else False
            chunked = (
                self._chunked is not None
                and self._chunked.wants(probe)
                and self._chunked.encode(
                    job_id=job_id,
                    source_key=source_key,
                    source_path=source_path,
                    probe=probe,
                    max_fps=max_fps,
                    output_path=output_path,
                    work_dir=temp_dir,
                )
            )
            if not chunked:
                self._encode_once(
                    source_path,
                    output_path,
                    max_fps=max_fps,
                    hdr=hdr,
                    job_id=job_id,
                    duration=probe.duration_seconds if probe else None,
                )
            if not output_path.is_file():
                raise ObjectStoreError(
                    "FFmpeg completed without producing an output file",
                    user_message=UNPROCESSABLE_VIDEO,
                )
            self._store.upload_file(key=output_key, source=str(output_path))
            return ProcessingResult(
                output_key=output_key,
                # The ladder job sizes its rungs from the stored height, so an
                # unprobed source gets no ladder, exactly as before.
                ladder_pending=self._builds_ladder and probe is not None,
                width=probe.width if probe else None,
                height=probe.height if probe else None,
                duration_seconds=probe.duration_seconds if probe else None,
                thumbnail_key=self._store_thumbnail(job_id=job_id, thumbnail_path=thumbnail_path),
            )
        finally:
            shutil.rmtree(temp_dir, ignore_errors=True)

    def _encode_once(
        self,
        source_path: Path,
        output_path: Path,
        *,
        max_fps: int,
        hdr: bool,
        job_id: UUID | None = None,
        duration: float | None = None,
    ):
        """The whole MP4 in one FFmpeg run, reporting how far it has got."""
        from app.worker.progress_watch import ProgressWatcher

        watching = self._progress is not None and job_id is not None and bool(duration)
        progress_file = output_path.with_name("progress.txt")
        command = [
            self._ffmpeg_binary,
            "-hide_banner",
            "-loglevel",
            "error",
            "-i",
            str(source_path),
            "-vf",
            ",".join(mp4_video_filters(max_fps, self._max_height, hdr)),
            *(BT709_TAGS if hdr else []),
            "-c:v",
            "libx264",
            "-preset",
            self._preset,
            "-crf",
            str(self._crf),
            "-c:a",
            "aac",
            "-movflags",
            "+faststart",
            *(["-progress", str(progress_file), "-nostats"] if watching else []),
            "-y",
            str(output_path),
        ]
        try:
            if watching:
                with ProgressWatcher(
                    progress_file, duration, lambda percent: self._progress(job_id, percent)
                ):
                    result = self._run_encode(command)
            else:
                result = self._run_encode(command)
        except subprocess.TimeoutExpired as exc:
            raise ObjectStoreError(
                f"transcoding timed out after {self._timeout_seconds} seconds",
                user_message=(
                    "processing took too long and was stopped after about "
                    f"{self._timeout_seconds // 60} minutes; "
                    "try a shorter or smaller video"
                ),
            ) from exc
        if result.returncode != 0:
            stderr_lines = (result.stderr or "").strip().splitlines()
            detail = "\n".join(stderr_lines[-5:])[:1000] or "no FFmpeg error output"
            # `detail` is up to 1000 characters of FFmpeg stderr, naming the
            # temp path it was working on. Diagnostic only.
            raise ObjectStoreError(f"FFmpeg failed: {detail}", user_message=UNPROCESSABLE_VIDEO)

    def _run_encode(self, command: list[str]):
        return self._runner(
            command,
            capture_output=True,
            text=True,
            timeout=self._timeout_seconds,
            check=False,
        )


class CopyProcessor:
    """The sprint 1 stand-in for transcoding: copy source -> output."""

    def __init__(self, store: ObjectStore, *, output_prefix: str) -> None:
        self._store = store
        self._output_prefix = output_prefix.strip("/")

    def output_key_for(self, *, job_id: UUID, source_key: str) -> str:
        filename = PurePosixPath(source_key).name or f"{job_id}.bin"
        return f"{self._output_prefix}/{job_id}/{filename}"

    def run(
        self, *, job_id: UUID, source_key: str, max_fps: int = DEFAULT_MAX_FPS
    ) -> ProcessingResult:
        if not self._store.object_exists(source_key):
            raise ObjectStoreError(
                f"source object missing from storage: {source_key}",
                user_message="the uploaded file is no longer in storage; please upload it again",
            )

        output_key = self.output_key_for(job_id=job_id, source_key=source_key)
        self._store.copy_object(source_key=source_key, output_key=output_key)
        # No ladder: this stand-in never decoded the video in the first place.
        return ProcessingResult(output_key=output_key)


@lru_cache
def get_processing_step() -> ProcessingStep:
    # Imported here, not at module level: both of these import this module,
    # so a top-level import would be a cycle.
    from functools import partial

    from app.worker.probe import probe_source
    from app.worker.thumbnail import extract_thumbnail
    from app.worker.validation import validate_source

    settings = get_settings()
    # The internal client: the worker only ever reads and writes objects.
    store = MinioObjectStore(internal_client(), bucket=bucket())
    return FfmpegProcessor(
        store,
        output_prefix=settings.worker_output_prefix,
        ffmpeg_binary=settings.worker_ffmpeg_binary,
        preset=settings.worker_ffmpeg_preset,
        crf=settings.worker_ffmpeg_crf,
        max_height=settings.worker_ffmpeg_max_height,
        timeout_seconds=settings.worker_ffmpeg_timeout_seconds,
        builds_ladder=True,
        prober=partial(
            probe_source,
            ffprobe_binary=settings.worker_ffprobe_binary,
            timeout_seconds=settings.worker_ffprobe_timeout_seconds,
        ),
        source_check=partial(
            validate_source, max_duration_seconds=settings.media_max_duration_seconds
        ),
        thumbnailer=partial(
            extract_thumbnail,
            ffmpeg_binary=settings.worker_ffmpeg_binary,
            max_width=settings.thumbnail_max_width,
            max_height=settings.thumbnail_max_height,
            timeout_seconds=settings.worker_ffprobe_timeout_seconds,
        ),
        chunked=_chunked_mp4(store, settings) if settings.worker_chunked_mp4 else None,
        progress=_progress_reporter(),
    )


def _progress_reporter():
    from functools import partial

    from app.queue import get_redis_connection
    from app.services.progress import set_progress

    return partial(set_progress, get_redis_connection())


def _chunked_mp4(store: "ObjectStore", settings):
    from app.queue import enqueue_helper, get_redis_connection
    from app.worker.chunked import ChunkedMp4

    return ChunkedMp4(
        store,
        get_redis_connection(),
        enqueue_helper=enqueue_helper,
        output_prefix=settings.worker_output_prefix,
        ffmpeg_binary=settings.worker_ffmpeg_binary,
        ffprobe_binary=settings.worker_ffprobe_binary,
        preset=settings.worker_ffmpeg_preset,
        crf=settings.worker_ffmpeg_crf,
        max_height=settings.worker_ffmpeg_max_height,
        chunk_seconds=settings.worker_chunk_seconds,
        min_duration_seconds=settings.worker_chunk_min_duration_seconds,
        helpers=settings.worker_chunk_helpers,
        stale_after_seconds=settings.worker_chunk_stale_seconds,
        timeout_seconds=settings.worker_ffmpeg_timeout_seconds,
        progress=_progress_reporter(),
        wake=_helper_waker(),
    )


def _helper_waker():
    from app.queue import get_redis_connection
    from app.services.worker_wake import wake_workers

    connection = get_redis_connection()
    return lambda workers: wake_workers(connection, at_least=workers)


@lru_cache
def get_ladder_step():
    """The HLS ladder builder, for `app.worker.tasks.build_ladder`."""
    from app.worker.hls import HlsLadderBuilder

    settings = get_settings()
    from functools import partial

    from app.worker.probe import probe_source

    return HlsLadderBuilder(
        MinioObjectStore(internal_client(), bucket=bucket()),
        prober=partial(
            probe_source,
            ffprobe_binary=settings.worker_ffprobe_binary,
            timeout_seconds=settings.worker_ffprobe_timeout_seconds,
        ),
        output_prefix=settings.worker_output_prefix,
        ffmpeg_binary=settings.worker_ffmpeg_binary,
        preset=settings.worker_ffmpeg_preset,
        timeout_seconds=settings.worker_ffmpeg_timeout_seconds,
        max_height=settings.worker_hls_max_height,
    )


def get_edit_processing_step(operations: list[dict]) -> ProcessingStep:
    """Build an uncached processor because operations belong to one job."""
    from functools import partial

    from app.worker.operations import EditProcessor
    from app.worker.probe import probe_source

    settings = get_settings()
    store = MinioObjectStore(internal_client(), bucket=bucket())
    return EditProcessor(
        store,
        operations,
        output_prefix=settings.worker_output_prefix,
        ffmpeg_binary=settings.worker_ffmpeg_binary,
        preset=settings.worker_ffmpeg_preset,
        crf=settings.worker_ffmpeg_crf,
        timeout_seconds=settings.worker_ffmpeg_timeout_seconds,
        prober=partial(
            probe_source,
            ffprobe_binary=settings.worker_ffprobe_binary,
            timeout_seconds=settings.worker_ffprobe_timeout_seconds,
        ),
    )
