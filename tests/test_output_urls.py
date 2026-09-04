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
    )
