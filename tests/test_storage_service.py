"""StorageService — B's MinIO client, without a live MinIO.

The existing tests/integration/test_storage.py proves it works against a real
server; these cover the branches and argument passing that an integration test
cannot pin down cheaply.
"""

import io

import pytest
from minio import Minio

from app.services import storage as storage_module
from app.services.storage import StorageService, get_storage_service


class FakeMinio:
    def __init__(self, *, exists: bool = True) -> None:
        self.exists = exists
        self.made: list[str] = []
        self.put: list[tuple] = []
        self.fput: list[tuple] = []
        self.fget: list[tuple] = []

    def bucket_exists(self, bucket):
        return self.exists

    def make_bucket(self, bucket):
        self.made.append(bucket)
        self.exists = True

    def put_object(self, bucket, key, stream, length, content_type=None):
        self.put.append((bucket, key, length, content_type))

    def fput_object(self, bucket, key, path):
        self.fput.append((bucket, key, path))

    def fget_object(self, bucket, key, path):
        self.fget.append((bucket, key, path))


@pytest.fixture
def client() -> FakeMinio:
    return FakeMinio()


def service(client) -> StorageService:
    return StorageService(client, bucket="videos")


async def test_ensure_bucket_creates_it_when_missing():
    client = FakeMinio(exists=False)

    await service(client).ensure_bucket()

    assert client.made == ["videos"]


async def test_ensure_bucket_is_a_no_op_when_it_already_exists(client):
    await service(client).ensure_bucket()

    assert client.made == []


async def test_upload_stream_passes_length_and_content_type(client):
    await service(client).upload_stream(
        "uploads/a/clip.mp4", io.BytesIO(b"abc"), length=3, content_type="video/mp4"
    )

    assert client.put == [("videos", "uploads/a/clip.mp4", 3, "video/mp4")]


async def test_upload_stream_defaults_the_content_type(client):
    await service(client).upload_stream("k", io.BytesIO(b"a"), length=1)

    assert client.put[0][3] == "application/octet-stream"


async def test_upload_stream_creates_the_bucket_first_on_a_cold_start():
    """First upload against an empty MinIO must not fail on a missing bucket."""
    client = FakeMinio(exists=False)

    await service(client).upload_stream("k", io.BytesIO(b"a"), length=1)

    assert client.made == ["videos"]
    assert client.put != []


async def test_upload_file_sends_the_local_path(client):
    await service(client).upload_file("/tmp/clip.mp4", "uploads/a/clip.mp4")

    assert client.fput == [("videos", "uploads/a/clip.mp4", "/tmp/clip.mp4")]


async def test_download_file_fetches_into_the_local_path(client):
    await service(client).download_file("outputs/a/clip.mp4", "/tmp/out.mp4")

    assert client.fget == [("videos", "outputs/a/clip.mp4", "/tmp/out.mp4")]


def test_get_storage_service_builds_a_minio_backed_service():
    get_storage_service.cache_clear()
    try:
        built = get_storage_service()
    finally:
        get_storage_service.cache_clear()

    assert isinstance(built, StorageService)
    assert isinstance(built._client, Minio)
    assert built._bucket == storage_module.get_settings().minio_bucket
