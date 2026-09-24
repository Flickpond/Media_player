"""StorageService — B's MinIO client, without a live MinIO.

The existing tests/integration/test_storage.py proves it works against a real
server; these cover the branches and argument passing that an integration test
cannot pin down cheaply.
"""

import io

import pytest
from minio import Minio

from app.services.minio_client import bucket
from app.services.storage import StorageService, get_storage_service


class FakeMinio:
    def __init__(self, *, exists: bool = True) -> None:
        self.exists = exists
        self.checked = 0
        self.made: list[str] = []
        self.put: list[tuple] = []
        self.fput: list[tuple] = []
        self.fget: list[tuple] = []

    def bucket_exists(self, bucket):
        self.checked += 1
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


async def test_uploading_does_not_check_the_bucket(client):
    """Deliberate change: this used to call bucket_exists before every write.

    That cost a round trip on a path the contract budgets under one second
    (N1), to re-answer a question whose answer cannot change between requests.
    The check moved to the application's startup hook. If a future edit puts it
    back on the write path, this fails.
    """
    await service(client).upload_stream("k", io.BytesIO(b"a"), length=1)
    await service(client).upload_file("/tmp/clip.mp4", "uploads/a/clip.mp4")

    assert client.checked == 0, "no bucket_exists round trip belongs on a write"
    assert client.made == [], "and certainly no bucket creation"
    assert client.put != [] and client.fput != [], "the writes still happened"


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
    assert built._bucket == bucket()


async def test_the_startup_hook_is_what_creates_the_bucket():
    """The other half of moving the check off the write path.

    A first upload against an empty MinIO still has to work, so something must
    create the bucket -- it is the application's lifespan now. Asserted here so
    the two halves cannot be removed independently.
    """
    from app.main import lifespan
    from app.services import storage as storage_module

    created = FakeMinio(exists=False)
    storage_module.get_storage_service.cache_clear()
    try:
        service_ = StorageService(created, bucket="videos")
        original = storage_module.get_storage_service
        storage_module.get_storage_service = lambda: service_
        import app.main as main_module

        main_module.get_storage_service = lambda: service_
        async with lifespan(None):
            pass
    finally:
        storage_module.get_storage_service = original
        storage_module.get_storage_service.cache_clear()

    assert created.made == ["videos"], "startup must create a missing bucket"
