from datetime import timedelta
from functools import lru_cache
from typing import Protocol

from minio import Minio
from starlette.concurrency import run_in_threadpool

from app.config import get_settings


class OutputUrlSigner(Protocol):
    async def create_url(self, output_key: str) -> str: ...


class MinioOutputUrlSigner:
    def __init__(
        self,
        client: Minio,
        *,
        bucket: str,
        expiry_seconds: int,
    ) -> None:
        self._client = client
        self._bucket = bucket
        self._expiry = timedelta(seconds=expiry_seconds)

    async def create_url(self, output_key: str) -> str:
        return await run_in_threadpool(
            self._client.presigned_get_object,
            self._bucket,
            output_key,
            expires=self._expiry,
        )


@lru_cache
def get_output_url_signer() -> OutputUrlSigner:
    settings = get_settings()
    client = Minio(
        settings.minio_public_endpoint,
        access_key=settings.minio_access_key,
        secret_key=settings.minio_secret_key,
        secure=settings.minio_use_ssl,
        region=settings.minio_region,
    )
    return MinioOutputUrlSigner(
        client,
        bucket=settings.minio_bucket,
        expiry_seconds=settings.output_url_expiry_seconds,
    )
