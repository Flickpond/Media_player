"""POST /upload — B's endpoint.

Checks the contract §3.2 obligations: 202 with a job id, the 100MB limit (N8,
tested *at* the limit as the plan asks), and that the row and the object agree
on one source_key.
"""

import io
from uuid import UUID

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient

from app.api import uploads as uploads_api
from app.api.uploads import MAX_FILE_SIZE, upload_video
from app.database import get_session
from app.main import create_app
from app.services.storage import get_storage_service


class FakeStorage:
    def __init__(self) -> None:
        self.uploads: list[dict] = []

    async def upload_stream(self, object_key, stream, *, length, content_type):
        self.uploads.append({"key": object_key, "length": length, "content_type": content_type})


class SizedFile(io.BytesIO):
    """Reports an arbitrary size without allocating it."""

    def __init__(self, size: int) -> None:
        super().__init__(b"")
        self._size = size

    def seek(self, offset, whence=0):
        return self._size if whence == 2 else super().seek(offset, whence)

    def tell(self):
        return self._size


class FakeUpload:
    def __init__(self, size: int, filename="clip.mp4", content_type="video/mp4"):
        self.file = SizedFile(size)
        self.filename = filename
        self.content_type = content_type


@pytest.fixture
def storage() -> FakeStorage:
    return FakeStorage()


@pytest.fixture
def events(monkeypatch: pytest.MonkeyPatch) -> list[tuple]:
    """Ordered log of side effects, so ordering can be asserted, not assumed."""
    log: list[tuple] = []

    async def fake_create_job(_session, *, job_id, filename, source_key):
        log.append(("insert", {"job_id": job_id, "filename": filename, "source_key": source_key}))

    def fake_enqueue(job_id):
        log.append(("enqueue", job_id))
        return str(job_id)

    monkeypatch.setattr(uploads_api, "create_job", fake_create_job)
    monkeypatch.setattr(uploads_api, "enqueue_job", fake_enqueue)
    return log


def inserts(events) -> list[dict]:
    """Rows the endpoint asked the repository to create, in order."""
    return [payload for kind, payload in events if kind == "insert"]


@pytest_asyncio.fixture
async def client(storage):
    application = create_app()

    async def fake_session():
        yield object()

    application.dependency_overrides[get_session] = fake_session
    application.dependency_overrides[get_storage_service] = lambda: storage
    transport = ASGITransport(app=application)
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        yield c


async def test_upload_returns_202_and_a_job_id(client, storage, events):
    response = await client.post(
        "/upload", files={"file": ("holiday.mp4", b"bytes-here", "video/mp4")}
    )

    assert response.status_code == 202
    body = response.json()
    assert set(body) == {"job_id"}
    UUID(body["job_id"])  # raises if it is not a uuid


async def test_object_key_and_row_agree_on_one_source_key(client, storage, events):
    """The worker reads source_key from the row; a mismatch here is a dead job."""
    response = await client.post(
        "/upload", files={"file": ("holiday.mp4", b"bytes-here", "video/mp4")}
    )

    job_id = response.json()["job_id"]
    assert inserts(events)[0]["source_key"] == f"uploads/{job_id}/holiday.mp4"
    assert storage.uploads[0]["key"] == inserts(events)[0]["source_key"]
    assert inserts(events)[0]["filename"] == "holiday.mp4"


async def test_content_type_is_passed_through_to_storage(client, storage, events):
    await client.post("/upload", files={"file": ("a.mp4", b"xyz", "video/mp4")})

    assert storage.uploads[0]["content_type"] == "video/mp4"
    assert storage.uploads[0]["length"] == 3


async def test_a_file_at_exactly_the_limit_is_accepted(storage, events):
    """N8: test *at* the limit, not just past it."""
    result = await upload_video(file=FakeUpload(MAX_FILE_SIZE), storage=storage, session=object())

    assert "job_id" in result
    assert len(storage.uploads) == 1


async def test_a_file_one_byte_over_the_limit_is_rejected(storage, events):
    response = await upload_video(
        file=FakeUpload(MAX_FILE_SIZE + 1), storage=storage, session=object()
    )

    assert response.status_code == 400
    import json

    assert json.loads(response.body) == {"error": "file too large"}


async def test_an_oversized_upload_is_not_stored_or_recorded(storage, events):
    """Rejecting late would leave an orphan object and a phantom row."""
    await upload_video(file=FakeUpload(MAX_FILE_SIZE + 1), storage=storage, session=object())

    assert storage.uploads == []
    assert inserts(events) == []


async def test_a_file_with_no_name_still_gets_a_key(storage, events):
    await upload_video(
        file=FakeUpload(10, filename=None, content_type=None), storage=storage, session=object()
    )

    assert inserts(events)[0]["filename"] == "upload.bin"
    assert storage.uploads[0]["content_type"] == "application/octet-stream"


# --- the enqueue step (the pipeline is dead without it) -------------------


async def test_upload_enqueues_the_job(client, storage, events):
    """Without this the row sits at `queued` forever and no worker ever runs."""
    response = await client.post("/upload", files={"file": ("a.mp4", b"xyz", "video/mp4")})

    job_id = response.json()["job_id"]
    assert ("enqueue", UUID(job_id)) in events


async def test_the_row_is_inserted_before_the_job_is_enqueued(client, storage, events):
    """Enqueue first and a fast worker can look up a row that does not exist yet."""
    await client.post("/upload", files={"file": ("a.mp4", b"xyz", "video/mp4")})

    kinds = [kind for kind, _ in events]
    assert kinds == ["insert", "enqueue"]


async def test_the_enqueued_id_matches_the_row_and_the_object(client, storage, events):
    response = await client.post("/upload", files={"file": ("a.mp4", b"xyz", "video/mp4")})

    job_id = UUID(response.json()["job_id"])
    inserted = [p for k, p in events if k == "insert"][0]
    enqueued = [p for k, p in events if k == "enqueue"][0]
    assert enqueued == job_id == inserted["job_id"]
    assert storage.uploads[0]["key"] == inserted["source_key"]


async def test_an_oversized_upload_enqueues_nothing(storage, events):
    await upload_video(file=FakeUpload(MAX_FILE_SIZE + 1), storage=storage, session=object())

    assert events == []
