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


class ObjectStoreError(RuntimeError):
    """Raised when the object store cannot serve a request.

    The message is written verbatim into the job's `error` column, so it has to
    read like something a user can act on.
    """


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
            raise ObjectStoreError(f"object store error checking {key}: {exc.code}") from exc
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
                f"copy failed from {source_key} to {output_key}: {exc.code}"
            ) from exc

    def download_file(self, *, key: str, destination: str) -> None:
        from minio.error import S3Error

        try:
            self._client.fget_object(self._bucket, key, destination)
        except S3Error as exc:
            raise ObjectStoreError(f"download failed for {key}: {exc.code}") from exc

    def upload_file(self, *, key: str, source: str) -> None:
        from minio.error import S3Error

        try:
            self._client.fput_object(self._bucket, key, source, content_type="video/mp4")
        except S3Error as exc:
            raise ObjectStoreError(f"upload failed for {key}: {exc.code}") from exc


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
            raise ObjectStoreError(f"source object missing from storage: {source_key}")

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
                    f"transcoding timed out after {self._timeout_seconds} seconds"
                ) from exc
            if result.returncode != 0:
                stderr_lines = (result.stderr or "").strip().splitlines()
                detail = "\n".join(stderr_lines[-5:])[:1000] or "no FFmpeg error output"
                raise ObjectStoreError(f"FFmpeg failed: {detail}")
            if not output_path.is_file():
                raise ObjectStoreError("FFmpeg completed without producing an output file")
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
            raise ObjectStoreError(f"source object missing from storage: {source_key}")

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
