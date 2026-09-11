from uuid import uuid4

import pytest
from minio.error import S3Error

from app.worker.storage import CopyProcessor, MinioObjectStore, ObjectStoreError

# A key that carries the two things a user must never be shown back: the
# internal layout, and the name of somebody else's file.
LEAKY_KEY = "uploads/9f3a/holiday-in-crete.mp4"


def s3_error(code: str) -> S3Error:
    return S3Error(
        response=None,
        code=code,
        message=code,
        resource="/videos/demo.mp4",
        request_id="req-1",
        host_id="host-1",
    )


class FakeStore:
    def __init__(self, *, exists: bool = True) -> None:
        self.exists = exists
        self.copies: list[tuple[str, str]] = []

    def object_exists(self, key: str) -> bool:
        return self.exists

    def copy_object(self, *, source_key: str, output_key: str) -> None:
        self.copies.append((source_key, output_key))


class FakeMinio:
    def __init__(self, *, stat_error=None, copy_error=None, fget_error=None, fput_error=None):
        self.stat_error = stat_error
        self.copy_error = copy_error
        self.fget_error = fget_error
        self.fput_error = fput_error
        self.copy_calls: list[tuple[str, str]] = []

    def fget_object(self, bucket, key, destination):
        if self.fget_error:
            raise self.fget_error

    def fput_object(self, bucket, key, source, content_type=None):
        if self.fput_error:
            raise self.fput_error

    def stat_object(self, bucket, key):
        if self.stat_error:
            raise self.stat_error
        return object()

    def copy_object(self, bucket, output_key, source):
        if self.copy_error:
            raise self.copy_error
        self.copy_calls.append((source.object_name, output_key))


def test_output_key_follows_the_agreed_convention():
    processor = CopyProcessor(FakeStore(), output_prefix="outputs")
    job_id = uuid4()

    key = processor.output_key_for(job_id=job_id, source_key=f"uploads/{job_id}/holiday.mp4")

    assert key == f"outputs/{job_id}/holiday.mp4"


def test_copy_processor_returns_the_output_key_it_wrote():
    store = FakeStore()
    processor = CopyProcessor(store, output_prefix="outputs")
    job_id = uuid4()

    key = processor.run(job_id=job_id, source_key="uploads/a/demo.mp4")

    assert store.copies == [("uploads/a/demo.mp4", key)]


def test_missing_source_object_raises_a_readable_error():
    processor = CopyProcessor(FakeStore(exists=False), output_prefix="outputs")

    with pytest.raises(ObjectStoreError, match="source object missing"):
        processor.run(job_id=uuid4(), source_key="uploads/gone.mp4")


def test_object_exists_is_false_for_a_missing_key():
    store = MinioObjectStore(FakeMinio(stat_error=s3_error("NoSuchKey")), bucket="videos")

    assert store.object_exists("uploads/gone.mp4") is False


def test_object_exists_surfaces_real_storage_faults():
    """A credentials or connectivity fault must not be read as 'file missing'."""
    store = MinioObjectStore(FakeMinio(stat_error=s3_error("AccessDenied")), bucket="videos")

    with pytest.raises(ObjectStoreError, match="AccessDenied"):
        store.object_exists("uploads/demo.mp4")


def test_object_exists_is_true_for_a_present_key():
    store = MinioObjectStore(FakeMinio(), bucket="videos")

    assert store.object_exists("uploads/demo.mp4") is True


def test_copy_object_wraps_s3_failures():
    store = MinioObjectStore(FakeMinio(copy_error=s3_error("InternalError")), bucket="videos")

    with pytest.raises(ObjectStoreError, match="copy failed"):
        store.copy_object(source_key="uploads/a.mp4", output_key="outputs/a.mp4")


def test_copy_object_uses_server_side_copy():
    client = FakeMinio()
    store = MinioObjectStore(client, bucket="videos")

    store.copy_object(source_key="uploads/a.mp4", output_key="outputs/a.mp4")

    assert client.copy_calls == [("uploads/a.mp4", "outputs/a.mp4")]


# --- P9: the key belongs in the log, never on the user's screen ------------


@pytest.mark.parametrize(
    "call",
    [
        pytest.param(lambda store: store.object_exists(LEAKY_KEY), id="object_exists"),
        pytest.param(
            lambda store: store.copy_object(source_key=LEAKY_KEY, output_key="outputs/x.mp4"),
            id="copy_object",
        ),
        pytest.param(
            lambda store: store.download_file(key=LEAKY_KEY, destination="/tmp/x"),
            id="download_file",
        ),
        pytest.param(
            lambda store: store.upload_file(key=LEAKY_KEY, source="/tmp/x"),
            id="upload_file",
        ),
    ],
)
def test_no_storage_failure_puts_an_object_key_in_the_users_half(call):
    """Every raise site in MinioObjectStore, swept rather than sampled.

    Asserting on both halves in one test is deliberate: a fix that scrubbed the
    user message by dropping the key entirely would pass a one-sided check and
    leave an operator with nothing to debug from.
    """
    fault = s3_error("AccessDenied")
    store = MinioObjectStore(
        FakeMinio(stat_error=fault, copy_error=fault, fget_error=fault, fput_error=fault),
        bucket="videos",
    )

    with pytest.raises(ObjectStoreError) as caught:
        call(store)

    assert LEAKY_KEY in str(caught.value), "the operator's half still needs the key"
    assert LEAKY_KEY not in caught.value.user_message
    assert "holiday-in-crete" not in caught.value.user_message
    assert "AccessDenied" not in caught.value.user_message
