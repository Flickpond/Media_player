"""S3 multipart control plane. Video bodies go directly from browsers to storage."""

from functools import lru_cache

import boto3
from botocore.config import Config
from botocore.exceptions import ClientError
from starlette.concurrency import run_in_threadpool

from app.config import get_settings
from app.services.media_type import SNIFF_LENGTH


class MultipartStorage:
    def __init__(self, internal, public, bucket):
        self.internal = internal
        self.public = public
        self.bucket = bucket

    async def call(self, operation, **kwargs):
        return await run_in_threadpool(
            getattr(self.internal, operation), Bucket=self.bucket, **kwargs
        )

    async def initiate(self, key):
        result = await self.call(
            "create_multipart_upload",
            Key=key,
            # Never serve attacker-selected HTML metadata, even before sniffing.
            ContentType="application/octet-stream",
            ContentDisposition="attachment",
        )
        return result["UploadId"]

    async def sign(self, upload, numbers, expires):
        def generate():
            return {
                str(n): self.public.generate_presigned_url(
                    "upload_part",
                    Params={
                        "Bucket": self.bucket,
                        "Key": upload.source_key,
                        "UploadId": upload.storage_id,
                        "PartNumber": n,
                    },
                    ExpiresIn=expires,
                    HttpMethod="PUT",
                )
                for n in numbers
            }

        return await run_in_threadpool(generate)

    async def parts(self, upload):
        def collect():
            pages = self.internal.get_paginator("list_parts").paginate(
                Bucket=self.bucket, Key=upload.source_key, UploadId=upload.storage_id
            )
            return [
                {"n": part["PartNumber"], "etag": part["ETag"], "size": part["Size"]}
                for page in pages
                for part in page.get("Parts", [])
            ]

        return await run_in_threadpool(collect)

    async def head(self, key):
        try:
            return await self.call("head_object", Key=key)
        except ClientError as exc:
            if exc.response["Error"]["Code"] in {"404", "NoSuchKey", "NotFound"}:
                return None
            raise

    async def complete(self, upload):
        return await self.call(
            "complete_multipart_upload",
            Key=upload.source_key,
            UploadId=upload.storage_id,
            MultipartUpload={
                "Parts": [{"PartNumber": p["n"], "ETag": p["etag"]} for p in upload.manifest]
            },
        )

    async def sniff(self, key):
        def read():
            response = self.internal.get_object(
                Bucket=self.bucket, Key=key, Range=f"bytes=0-{SNIFF_LENGTH - 1}"
            )
            try:
                return response["Body"].read(SNIFF_LENGTH)
            finally:
                response["Body"].close()

        return await run_in_threadpool(read)

    async def abort(self, upload):
        if upload.storage_id:
            try:
                await self.call(
                    "abort_multipart_upload", Key=upload.source_key, UploadId=upload.storage_id
                )
            except ClientError as exc:
                if exc.response["Error"]["Code"] != "NoSuchUpload":
                    raise
        # Also removes a merged object when complete crashed before creating a job.
        await self.call("delete_object", Key=upload.source_key)


@lru_cache
def get_multipart_storage():
    settings = get_settings()
    credentials = (
        {}
        if settings.storage_use_instance_role
        else {
            "aws_access_key_id": settings.minio_access_key,
            "aws_secret_access_key": settings.minio_secret_key,
        }
    )
    config = Config(
        signature_version="s3v4",
        s3={"addressing_style": "path"},
        connect_timeout=5,
        read_timeout=60,
        retries={"max_attempts": 2},
        request_checksum_calculation="when_required",
        response_checksum_validation="when_required",
    )

    def client(endpoint, secure):
        return boto3.client(
            "s3",
            endpoint_url=f"{'https' if secure else 'http'}://{endpoint}",
            region_name=settings.minio_region,
            config=config,
            **credentials,
        )

    return MultipartStorage(
        client(settings.minio_endpoint, settings.minio_use_ssl),
        client(settings.minio_public_endpoint, settings.minio_public_use_ssl),
        settings.minio_bucket,
    )
