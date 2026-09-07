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

# A real ISO base media file signature: a 32-byte `ftyp` box with the `isom`
# brand. The endpoint sniffs the head of every upload, so test payloads have to
# be something a video container check actually recognises.
MP4_HEAD = b"\x00\x00\x00\x20ftypisom\x00\x00\x02\x00isomiso2avc1mp41"
WEBM_HEAD = b"\x1a\x45\xdf\xa3\x01\x00\x00\x00\x00\x00\x00\x23B\x82\x84webm" + b"\x00" * 16
# A real PDF, which is the case a declared-type check cannot catch: renamed to
# .mp4, a browser will declare it video/mp4 from the extension.
PDF_HEAD = b"%PDF-1.4\n1 0 obj<</Type/Catalog>>endobj\ntrailer<</Root 1 0 R>>\n%%EOF\n"


class FakeStorage:
    def __init__(self) -> None:
        self.uploads: list[dict] = []

    async def upload_stream(self, object_key, stream, *, length, content_type):
        self.uploads.append({"key": object_key, "length": length, "content_type": content_type})


class SizedFile(io.BytesIO):
    """Reports an arbitrary size without allocating it.

    The content is a real MP4 signature even when the reported size is huge:
    the endpoint sniffs the head of the stream, so empty bytes would be
    rejected as "not a video" before any size assertion could run.
    """

    def __init__(self, size: int, content: bytes = MP4_HEAD) -> None:
        super().__init__(content)
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
    response = await client.post("/upload", files={"file": ("holiday.mp4", MP4_HEAD, "video/mp4")})

    assert response.status_code == 202
    body = response.json()
    assert set(body) == {"job_id"}
    UUID(body["job_id"])  # raises if it is not a uuid


async def test_object_key_and_row_agree_on_one_source_key(client, storage, events):
    """The worker reads source_key from the row; a mismatch here is a dead job."""
    response = await client.post("/upload", files={"file": ("holiday.mp4", MP4_HEAD, "video/mp4")})

    job_id = response.json()["job_id"]
    assert inserts(events)[0]["source_key"] == f"uploads/{job_id}/holiday.mp4"
    assert storage.uploads[0]["key"] == inserts(events)[0]["source_key"]
    assert inserts(events)[0]["filename"] == "holiday.mp4"


async def test_the_stored_content_type_comes_from_the_bytes(client, storage, events):
    await client.post("/upload", files={"file": ("a.mp4", MP4_HEAD, "video/mp4")})

    assert storage.uploads[0]["content_type"] == "video/mp4"
    assert storage.uploads[0]["length"] == len(MP4_HEAD)


async def test_a_file_at_exactly_the_limit_is_accepted(storage, events):
    """N8: test *at* the limit, not just past it."""
    result = await upload_video(file=FakeUpload(MAX_FILE_SIZE), storage=storage, session=object())

    assert "job_id" in result
    assert len(storage.uploads) == 1


async def test_a_file_one_byte_over_the_limit_is_rejected(storage, events):
    response = await upload_video(
        file=FakeUpload(MAX_FILE_SIZE + 1), storage=storage, session=object()
    )

    assert response.status_code == 413
    import json

    assert json.loads(response.body) == {"error": "file too large"}


async def test_an_oversized_upload_is_not_stored_or_recorded(storage, events):
    """Rejecting late would leave an orphan object and a phantom row."""
    await upload_video(file=FakeUpload(MAX_FILE_SIZE + 1), storage=storage, session=object())

    assert storage.uploads == []
    assert inserts(events) == []


async def test_a_file_with_no_name_still_gets_a_key(storage, events):
    await upload_video(file=FakeUpload(10, filename=None), storage=storage, session=object())

    assert inserts(events)[0]["filename"] == "upload.bin"


# --- what may be stored, and under what key ------------------------------


async def test_a_non_video_content_type_is_refused(client, storage, events):
    """An HTML upload served back under text/html is stored XSS on the object
    store's origin. The type arrives in a header the client writes, so the
    picker's accept="video/*" proves nothing.
    """
    response = await client.post("/upload", files={"file": ("x.html", b"<script>", "text/html")})

    assert response.status_code == 415
    assert response.json() == {"error": "unsupported media type"}
    assert storage.uploads == []
    assert events == []


async def test_a_content_type_with_parameters_is_still_matched(client, storage, events):
    response = await client.post(
        "/upload", files={"file": ("a.mp4", MP4_HEAD, "video/mp4; charset=binary")}
    )

    assert response.status_code == 202
    assert storage.uploads[0]["content_type"] == "video/mp4"


async def test_a_pdf_renamed_to_mp4_is_refused(client, storage, events):
    """The case the declared-type check cannot catch. A browser fills in the
    content type from the file extension, so renaming doc.pdf to doc.mp4 makes
    it arrive declared video/mp4. Only the bytes give it away.
    """
    response = await client.post("/upload", files={"file": ("doc.mp4", PDF_HEAD, "video/mp4")})

    assert response.status_code == 415
    assert response.json() == {"error": "file content is not a recognized video format"}
    assert storage.uploads == []
    assert events == []


async def test_html_disguised_as_mp4_is_refused(client, storage, events):
    body = b"<html><body><script>alert(document.domain)</script></body></html>"

    response = await client.post("/upload", files={"file": ("clip.mp4", body, "video/mp4")})

    assert response.status_code == 415
    assert storage.uploads == []


async def test_a_container_that_disagrees_with_the_declared_type_is_stored_as_itself(
    client, storage, events
):
    """Browsers routinely mislabel a container by extension, so a mismatch is
    not treated as an attack -- the bytes simply win.
    """
    response = await client.post("/upload", files={"file": ("a.mp4", WEBM_HEAD, "video/mp4")})

    assert response.status_code == 202
    assert storage.uploads[0]["content_type"] == "video/webm"


async def test_a_file_too_short_to_identify_is_refused(client, storage, events):
    response = await client.post("/upload", files={"file": ("a.mp4", b"\x00\x00\x01", "video/mp4")})

    assert response.status_code == 415
    assert storage.uploads == []


async def test_the_stream_is_rewound_after_sniffing(client, storage, events):
    """Sniffing reads from the same handle the upload streams from; leaving the
    cursor past the header would silently truncate every stored object.
    """
    captured = {}

    async def capture(object_key, stream, *, length, content_type):
        captured["position"] = stream.tell()
        captured["body"] = stream.read()
        storage.uploads.append({"key": object_key, "length": length, "content_type": content_type})

    storage.upload_stream = capture

    await client.post("/upload", files={"file": ("a.mp4", MP4_HEAD, "video/mp4")})

    assert captured["position"] == 0
    assert captured["body"] == MP4_HEAD


@pytest.mark.parametrize(
    ("sent", "stored"),
    [
        ("../../etc/passwd", "passwd"),
        ("../../../outputs/other/clip.mp4", "clip.mp4"),
        ("a/b/c/clip.mp4", "clip.mp4"),
        ("..", "upload.bin"),
        ("my clip;rm -rf.mp4", "my_clip_rm_-rf.mp4"),
        (".hidden.mp4", "hidden.mp4"),
    ],
)
async def test_the_key_never_escapes_the_jobs_own_prefix(storage, events, sent, stored):
    """The filename comes from the multipart headers, so it is attacker input."""
    await upload_video(file=FakeUpload(10, filename=sent), storage=storage, session=object())

    job_id = inserts(events)[0]["job_id"]
    assert storage.uploads[0]["key"] == f"uploads/{job_id}/{stored}"


async def test_a_very_long_filename_is_truncated(storage, events):
    await upload_video(
        file=FakeUpload(10, filename="a" * 500 + ".mp4"), storage=storage, session=object()
    )

    assert len(inserts(events)[0]["filename"]) == 100


async def test_an_oversized_content_length_is_refused_before_the_body_is_read(client, storage):
    """The handler's own check runs only after Starlette has spooled the whole
    body to disk, so the guard that actually protects the disk is this one.
    """
    response = await client.post(
        "/upload",
        content=b"not-really-that-big",
        headers={
            "content-type": "video/mp4",
            "content-length": str(MAX_FILE_SIZE + 1),
        },
    )

    assert response.status_code == 413
    assert response.json() == {"error": "file too large"}
    assert storage.uploads == []


# --- the enqueue step (the pipeline is dead without it) -------------------


async def test_upload_enqueues_the_job(client, storage, events):
    """Without this the row sits at `queued` forever and no worker ever runs."""
    response = await client.post("/upload", files={"file": ("a.mp4", MP4_HEAD, "video/mp4")})

    job_id = response.json()["job_id"]
    assert ("enqueue", UUID(job_id)) in events


async def test_the_row_is_inserted_before_the_job_is_enqueued(client, storage, events):
    """Enqueue first and a fast worker can look up a row that does not exist yet."""
    await client.post("/upload", files={"file": ("a.mp4", MP4_HEAD, "video/mp4")})

    kinds = [kind for kind, _ in events]
    assert kinds == ["insert", "enqueue"]


async def test_the_enqueued_id_matches_the_row_and_the_object(client, storage, events):
    response = await client.post("/upload", files={"file": ("a.mp4", MP4_HEAD, "video/mp4")})

    job_id = UUID(response.json()["job_id"])
    inserted = [p for k, p in events if k == "insert"][0]
    enqueued = [p for k, p in events if k == "enqueue"][0]
    assert enqueued == job_id == inserted["job_id"]
    assert storage.uploads[0]["key"] == inserted["source_key"]


async def test_an_oversized_upload_enqueues_nothing(storage, events):
    await upload_video(file=FakeUpload(MAX_FILE_SIZE + 1), storage=storage, session=object())

    assert events == []
