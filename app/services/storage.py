from datetime import datetime
from functools import lru_cache
from typing import BinaryIO

from minio import Minio
from starlette.concurrency import run_in_threadpool

from app.services.minio_client import bucket, internal_client


class StorageService:
    def __init__(
        self,
        client: Minio,
        *,
        bucket: str,
    ) -> None:
        self._client = client
        self._bucket = bucket

    async def ensure_bucket(self) -> None:
        exists = await run_in_threadpool(
            self._client.bucket_exists,
            self._bucket,
        )

        if not exists:
            await run_in_threadpool(
                self._client.make_bucket,
                self._bucket,
            )

    async def upload_file(
        self,
        local_path: str,
        object_key: str,
    ) -> None:
        await self.ensure_bucket()

        await run_in_threadpool(
            self._client.fput_object,
            self._bucket,
            object_key,
            local_path,
        )

    async def download_file(
        self,
        object_key: str,
        local_path: str,
    ) -> None:
        await run_in_threadpool(
            self._client.fget_object,
            self._bucket,
            object_key,
            local_path,
        )

    async def delete_object(self, object_key: str) -> None:
        await run_in_threadpool(self._client.remove_object, self._bucket, object_key)

    async def list_objects(self, prefix: str = "") -> list[tuple[str, datetime | None]]:
        def collect() -> list[tuple[str, datetime | None]]:
            return [
                (item.object_name, item.last_modified)
                for item in self._client.list_objects(
                    self._bucket, prefix=prefix, recursive=True
                )
            ]

        return await run_in_threadpool(collect)

    async def upload_stream(
        self,
        object_key: str,
        stream: BinaryIO,
        *,
        length: int,
        content_type: str = "application/octet-stream",
    ) -> None:
        await self.ensure_bucket()

        await run_in_threadpool(
            self._client.put_object,
            self._bucket,
            object_key,
            stream,
            length,
            content_type=content_type,
        )


@lru_cache
def get_storage_service() -> StorageService:
    # The internal client: this runs inside the compose network and never
    # produces a URL anyone else opens. See app/services/minio_client.py.
    return StorageService(internal_client(), bucket=bucket())