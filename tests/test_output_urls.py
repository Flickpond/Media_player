from datetime import timedelta
from unittest.mock import Mock

import pytest

from app.services.output_urls import MinioOutputUrlSigner


@pytest.mark.asyncio
async def test_minio_signer_uses_bucket_key_and_expiry() -> None:
    client = Mock()
    client.presigned_get_object.return_value = "https://minio.example.test/signed"
    signer = MinioOutputUrlSigner(client, bucket="videos", expiry_seconds=900)

    result = await signer.create_url("outputs/job-id/demo.mp4")

    assert result == "https://minio.example.test/signed"
    client.presigned_get_object.assert_called_once_with(
        "videos",
        "outputs/job-id/demo.mp4",
        expires=timedelta(seconds=900),
        response_headers={"response-content-disposition": "attachment"},
    )


@pytest.mark.asyncio
async def test_signed_urls_ask_for_a_download_not_an_inline_render() -> None:
    """Second layer behind the upload allowlist: whatever ends up in the bucket
    is saved by the browser rather than executed on the object store's origin.
    """
    client = Mock()
    signer = MinioOutputUrlSigner(client, bucket="videos", expiry_seconds=900)

    await signer.create_url("outputs/job-id/demo.mp4")

    headers = client.presigned_get_object.call_args.kwargs["response_headers"]
    assert headers["response-content-disposition"] == "attachment"
