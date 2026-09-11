"""Worker storage seam and processing implementations.

The worker downloads source objects, processes them, and uploads outputs through
this module. Keeping that seam separate from the job state machine lets Sprint 2
replace the copy stand-in with FFmpeg without changing task orchestration.
"""

import shutil
import subprocess
import tempfile
from functools import lru_cache
from pathlib import Path, PurePosixPath
from typing import Protocol
from uuid import UUID

from minio import Minio
from minio.commonconfig import CopySource

from app.config import get_settings
from app.services.minio_client import bucket, internal_client

# One message for every "the file itself is the problem" case. FFmpeg's own
# stderr names the temp path it was working on and assumes the reader knows what
# a codec is; neither belongs on the screen of someone who uploaded a holiday
# video.
UNPROCESSABLE_VIDEO = (
    "the video could not be processed; it may be corrupt or in a format we cannot read"
)


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

    def upload_file(self, *, key: str, source: str) -> None: ...


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

    def upload_file(self, *, key: str, source: str) -> None:
        from minio.error import S3Error

        try:
            self._client.fput_object(self._bucket, key, source, content_type="video/mp4")
        except S3Error as exc:
            raise ObjectStoreError(
                f"upload failed for {key}: {exc.code}",
                user_message="the processed video could not be saved; please try again",
            ) from exc


class ProcessingStep(Protocol):
    def run(self, *, job_id: UUID, source_key: str) -> str:
        """Process the source object and return the resulting output key."""


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
        max_height: int = 720,
        timeout_seconds: int = 870,
        runner=subprocess.run,
    ) -> None:
        self._store = store
        self._output_prefix = output_prefix.strip("/")
        self._ffmpeg_binary = ffmpeg_binary
        self._preset = preset
        self._crf = crf
        self._max_height = max_height
        self._timeout_seconds = timeout_seconds
        self._runner = runner

    def output_key_for(self, *, job_id: UUID, source_key: str) -> str:
        stem = PurePosixPath(source_key).stem or str(job_id)
        return f"{self._output_prefix}/{job_id}/{stem}.mp4"

    def run(self, *, job_id: UUID, source_key: str) -> str:
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
            command = [
                self._ffmpeg_binary,
                "-hide_banner",
                "-loglevel",
                "error",
                "-i",
                str(source_path),
                "-vf",
                f"scale=-2:min({self._max_height}\\,ih)",
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
                "-y",
                str(output_path),
            ]
            try:
                result = self._runner(
                    command,
                    capture_output=True,
                    text=True,
                    timeout=self._timeout_seconds,
                    check=False,
                )
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
            if not output_path.is_file():
                raise ObjectStoreError(
                    "FFmpeg completed without producing an output file",
                    user_message=UNPROCESSABLE_VIDEO,
                )
            self._store.upload_file(key=output_key, source=str(output_path))
            return output_key
        finally:
            shutil.rmtree(temp_dir, ignore_errors=True)


class CopyProcessor:
    """The sprint 1 stand-in for transcoding: copy source -> output."""

    def __init__(self, store: ObjectStore, *, output_prefix: str) -> None:
        self._store = store
        self._output_prefix = output_prefix.strip("/")

    def output_key_for(self, *, job_id: UUID, source_key: str) -> str:
        filename = PurePosixPath(source_key).name or f"{job_id}.bin"
        return f"{self._output_prefix}/{job_id}/{filename}"

    def run(self, *, job_id: UUID, source_key: str) -> str:
        if not self._store.object_exists(source_key):
            raise ObjectStoreError(
                f"source object missing from storage: {source_key}",
                user_message="the uploaded file is no longer in storage; please upload it again",
            )

        output_key = self.output_key_for(job_id=job_id, source_key=source_key)
        self._store.copy_object(source_key=source_key, output_key=output_key)
        return output_key


@lru_cache
def get_processing_step() -> ProcessingStep:
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
    )
