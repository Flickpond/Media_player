from datetime import timedelta
from functools import lru_cache
from typing import Protocol

from minio import Minio
from starlette.concurrency import run_in_threadpool

from app.config import get_settings
from app.services.minio_client import bucket, public_client


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
        # Ask the object store to serve the object as a download rather than
        # rendering it inline. Uploads are already restricted to video types,
        # so this is the second layer: if something non-video ever reaches a
        # bucket, opening its URL saves a file instead of executing it on the
        # object store's origin. A <video> element ignores the header, so the
        # player still plays the result.
        return await run_in_threadpool(
            self._client.presigned_get_object,
            self._bucket,
            output_key,
            expires=self._expiry,
            response_headers={"response-content-disposition": "attachment"},
        )


@lru_cache
def get_output_url_signer() -> OutputUrlSigner:
    # The *public* client, deliberately not the internal one: the host it is
    # built with lands inside the SigV4 signature, and the URL is opened by a
    # browser. Swapping this for internal_client() breaks playback silently.
    return MinioOutputUrlSigner(
        public_client(),
        bucket=bucket(),
        expiry_seconds=get_settings().output_url_expiry_seconds,
    )
