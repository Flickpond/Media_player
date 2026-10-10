import io
from types import SimpleNamespace
from unittest.mock import Mock
from urllib.parse import parse_qs, urlsplit

import boto3
import pytest
from botocore.config import Config
from botocore.exceptions import ClientError
from botocore.stub import Stubber

from app.services.multipart_storage import MultipartStorage


@pytest.fixture
def storage():
    client = boto3.client(
        "s3",
        endpoint_url="http://storage.test:9000",
        region_name="us-east-1",
        aws_access_key_id="test-key",
        aws_secret_access_key="test-secret",
        config=Config(signature_version="s3v4"),
    )
    return MultipartStorage(client, client, "videos")


async def test_initiate_never_trusts_browser_content_type(storage):
    with Stubber(storage.internal) as stub:
        stub.add_response(
            "create_multipart_upload",
            {"UploadId": "s3-id"},
            {
                "Bucket": "videos",
                "Key": "uploads/test/file",
                "ContentType": "application/octet-stream",
                "ContentDisposition": "attachment",
            },
        )
        assert await storage.initiate("uploads/test/file") == "s3-id"
        stub.assert_no_pending_responses()


async def test_signed_put_uses_public_host_part_number_and_upload_id(storage):
    upload = SimpleNamespace(source_key="uploads/test/file", storage_id="s3-id")
    urls = await storage.sign(upload, [1, 4], 900)
    for n, url in urls.items():
        parsed = urlsplit(url)
        assert parsed.netloc == "storage.test:9000"
        query = parse_qs(parsed.query)
        assert query["partNumber"] == [n]
        assert query["uploadId"] == ["s3-id"]
        assert query["X-Amz-Expires"] == ["900"]
        assert query["X-Amz-Signature"]


async def test_list_parts_follows_pagination(storage):
    upload = SimpleNamespace(source_key="key", storage_id="s3-id")
    with Stubber(storage.internal) as stub:
        stub.add_response(
            "list_parts",
            {
                "IsTruncated": True,
                "NextPartNumberMarker": 1,
                "Parts": [{"PartNumber": 1, "ETag": '"one"', "Size": 16}],
            },
            {"Bucket": "videos", "Key": "key", "UploadId": "s3-id"},
        )
        stub.add_response(
            "list_parts",
            {
                "IsTruncated": False,
                "Parts": [{"PartNumber": 2, "ETag": '"two"', "Size": 2}],
            },
            {"Bucket": "videos", "Key": "key", "UploadId": "s3-id", "PartNumberMarker": 1},
        )
        assert await storage.parts(upload) == [
            {"n": 1, "etag": '"one"', "size": 16},
            {"n": 2, "etag": '"two"', "size": 2},
        ]


async def test_sniff_only_reads_prefix_and_closes_response():
    body = io.BytesIO(b"x" * 8000)
    client = Mock()
    client.get_object.return_value = {"Body": body}
    store = MultipartStorage(client, client, "videos")
    assert len(await store.sniff("key")) == 4096
    client.get_object.assert_called_once_with(Bucket="videos", Key="key", Range="bytes=0-4095")
    assert body.closed


async def test_head_does_not_treat_permission_failure_as_absent(storage):
    with Stubber(storage.internal) as stub:
        stub.add_client_error("head_object", service_error_code="404", http_status_code=404)
        stub.add_client_error(
            "head_object", service_error_code="AccessDenied", http_status_code=403
        )
        assert await storage.head("key") is None
        with pytest.raises(ClientError):
            await storage.head("key")


async def test_abort_also_cleans_a_previously_merged_object(storage):
    upload = SimpleNamespace(source_key="key", storage_id="s3-id")
    with Stubber(storage.internal) as stub:
        stub.add_client_error(
            "abort_multipart_upload", service_error_code="NoSuchUpload", http_status_code=404
        )
        stub.add_response("delete_object", {}, {"Bucket": "videos", "Key": "key"})
        await storage.abort(upload)
        stub.assert_no_pending_responses()
