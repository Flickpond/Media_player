from functools import lru_cache

from minio import Minio
from starlette.concurrency import run_in_threadpool

from app.config import get_settings


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


@lru_cache
def get_storage_service() -> StorageService:
    settings = get_settings()

    client = Minio(
        settings.minio_endpoint,
        access_key=settings.minio_access_key,
        secret_key=settings.minio_secret_key,
        secure=settings.minio_use_ssl,
        region=settings.minio_region,
    )

    return StorageService(
        client,
        bucket=settings.minio_bucket,
    )