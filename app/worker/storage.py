"""Object-store access for the processing step.

Sprint 1 does not transcode. The "processing" step is a server-side object copy
standing in for FFmpeg, which keeps the pipeline shape honest -- read from
object storage, write a new object, record the key -- without the encode.

`ObjectStore` is the seam. When B's storage client lands it only has to satisfy
this protocol; nothing in `tasks.py` changes. Sprint 2 swaps `CopyProcessor`
for a real FFmpeg step behind the same `ProcessingStep` protocol.
"""

from functools import lru_cache
from pathlib import PurePosixPath
from typing import Protocol
from uuid import UUID

from minio import Minio
from minio.commonconfig import CopySource

from app.config import get_settings


class ObjectStoreError(RuntimeError):
    """Raised when the object store cannot serve a request.

    The message is written verbatim into the job's `error` column, so it has to
    read like something a user can act on.
    """


class ObjectStore(Protocol):
    def object_exists(self, key: str) -> bool: ...

    def copy_object(self, *, source_key: str, output_key: str) -> None: ...


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


class ProcessingStep(Protocol):
    def run(self, *, job_id: UUID, source_key: str) -> str:
        """Process the source object and return the resulting output key."""


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
    client = Minio(
        settings.minio_endpoint,
        access_key=settings.minio_access_key,
        secret_key=settings.minio_secret_key,
        secure=settings.minio_use_ssl,
        region=settings.minio_region,
    )
    store = MinioObjectStore(client, bucket=settings.minio_bucket)
    return CopyProcessor(store, output_prefix=settings.worker_output_prefix)
