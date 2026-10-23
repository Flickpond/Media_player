"""The seek-bar preview through the API: `preview_vtt_url` and its route."""

from datetime import UTC, datetime
from uuid import UUID, uuid4

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient

from app.api import jobs as jobs_api
from app.api import preview as preview_api
from app.database import get_session
from app.main import create_app
from app.models.job import HlsStatus, Job, JobStatus
from app.services.output_urls import get_output_url_signer
from app.services.storage import get_storage_service
from tests.conftest import authenticate_as

PREVIEW_KEY = "outputs/abc/preview/sprite.vtt"
INDEX = (
    b"WEBVTT\n\n"
    b"00:00:00.000 --> 00:00:05.000\nsprite.jpg#xywh=0,0,160,90\n\n"
    b"00:00:05.000 --> 00:00:07.500\nsprite.jpg#xywh=160,0,160,90\n"
)


class FakeSigner:
    def __init__(self) -> None:
        self.inline_keys: list[str] = []

    async def create_url(self, output_key: str) -> str:
        return f"https://media.example.test/{output_key}?disposition=attachment"

    async def create_inline_url(self, output_key: str) -> str:
        self.inline_keys.append(output_key)
        return f"https://cdn.example.test/{output_key}?Expires=1&Signature=a~b&Key-Pair-Id=K"


class FakeStorage:
    def __init__(self, objects: dict[str, bytes] | None = None) -> None:
        self.objects = {PREVIEW_KEY: INDEX} if objects is None else objects

    async def read_object(self, object_key: str) -> bytes:
        if object_key not in self.objects:
            raise KeyError(object_key)
        return self.objects[object_key]


@pytest_asyncio.fixture
async def signer():
    return FakeSigner()


@pytest_asyncio.fixture
async def storage():
    return FakeStorage()


@pytest_asyncio.fixture
async def client(test_user, signer: FakeSigner, storage: FakeStorage):
    application = create_app()

    async def fake_session():
        yield object()

    application.dependency_overrides[get_session] = fake_session
    application.dependency_overrides[get_output_url_signer] = lambda: signer
    application.dependency_overrides[get_storage_service] = lambda: storage
    authenticate_as(application, test_user)
    transport = ASGITransport(app=application)
    async with AsyncClient(transport=transport, base_url="http://test") as test_client:
        yield test_client


def make_job(
    *,
    status: JobStatus = JobStatus.DONE,
    preview_key: str | None = PREVIEW_KEY,
) -> Job:
    now = datetime.now(UTC)
    done = status == JobStatus.DONE
    return Job(
        id=uuid4(),
        filename="demo.mp4",
        status=status.value,
        source_key="uploads/demo.mp4",
        output_key="outputs/abc/demo.mp4" if done else None,
        hls_key="outputs/abc/hls/master.m3u8" if done else None,
        hls_status=(HlsStatus.READY if done else HlsStatus.PENDING).value,
        preview_key=preview_key if done else None,
        max_fps=30,
        created_at=now,
        updated_at=now,
    )


def serve(monkeypatch: pytest.MonkeyPatch, job: Job | None) -> None:
    async def fake_get_job(_session, _job_id: UUID, *, owner_id=None):
        serve.owner_id = owner_id
        return job

    monkeypatch.setattr(preview_api, "get_job", fake_get_job)
    monkeypatch.setattr(jobs_api, "get_job", fake_get_job)


# --- preview_vtt_url in the job shape ------------------------------------------


@pytest.mark.asyncio
async def test_a_video_with_a_preview_names_its_index_through_the_api(
    client: AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    job = make_job()
    serve(monkeypatch, job)

    body = (await client.get(f"/jobs/{job.id}")).json()

    # Through nginx's /api/ prefix, like hls_url: the browser fetches it.
    assert body["preview_vtt_url"] == f"/api/jobs/{job.id}/preview.vtt"


@pytest.mark.asyncio
async def test_a_video_without_a_preview_has_no_preview_url_at_all(
    client: AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Absent, not null: the player turns the preview on when the field is there."""
    job = make_job(preview_key=None)
    serve(monkeypatch, job)

    body = (await client.get(f"/jobs/{job.id}")).json()

    assert "preview_vtt_url" not in body
    assert "hls_url" in body


@pytest.mark.asyncio
async def test_a_job_still_processing_has_no_preview_url(
    client: AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    job = make_job(status=JobStatus.PROCESSING)
    serve(monkeypatch, job)

    body = (await client.get(f"/jobs/{job.id}")).json()

    assert "preview_vtt_url" not in body


# --- the index route ------------------------------------------------------------


@pytest.mark.asyncio
async def test_the_index_names_a_freshly_signed_sheet_and_keeps_each_rectangle(
    client: AsyncClient, signer: FakeSigner, monkeypatch: pytest.MonkeyPatch, test_user
) -> None:
    job = make_job()
    serve(monkeypatch, job)

    response = await client.get(f"/jobs/{job.id}/preview.vtt")

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/vtt")
    assert response.headers["cache-control"] == "private, no-store"
    sheet = (
        "https://cdn.example.test/outputs/abc/preview/sprite.jpg"
        "?Expires=1&Signature=a~b&Key-Pair-Id=K"
    )
    assert response.text == (
        "WEBVTT\n\n"
        f"00:00:00.000 --> 00:00:05.000\n{sheet}#xywh=0,0,160,90\n\n"
        f"00:00:05.000 --> 00:00:07.500\n{sheet}#xywh=160,0,160,90\n"
    )
    # Signed once however many cues name it, and only after the owner check.
    assert signer.inline_keys == ["outputs/abc/preview/sprite.jpg"]
    assert serve.owner_id == test_user.id


@pytest.mark.asyncio
async def test_only_a_bare_sheet_name_in_the_preview_folder_is_ever_signed(
    client: AsyncClient, signer: FakeSigner, storage: FakeStorage, monkeypatch: pytest.MonkeyPatch
) -> None:
    storage.objects[PREVIEW_KEY] = (
        b"WEBVTT\n\n00:00:00.000 --> 00:00:05.000\n../../other/sprite.jpg#xywh=0,0,160,90\n"
    )
    job = make_job()
    serve(monkeypatch, job)

    response = await client.get(f"/jobs/{job.id}/preview.vtt")

    assert response.status_code == 200
    assert signer.inline_keys == []
    assert "../../other/sprite.jpg#xywh" in response.text


@pytest.mark.parametrize(
    "job",
    [
        None,  # unknown, or another owner's
        make_job(preview_key=None),
        make_job(status=JobStatus.PROCESSING),
    ],
    ids=["not-yours", "no-preview", "not-done"],
)
@pytest.mark.asyncio
async def test_no_preview_to_serve_is_a_plain_404(
    client: AsyncClient, signer: FakeSigner, monkeypatch: pytest.MonkeyPatch, job
) -> None:
    serve(monkeypatch, job)

    response = await client.get(f"/jobs/{uuid4()}/preview.vtt")

    assert response.status_code == 404
    assert response.json() == {"error": "not found"}
    assert signer.inline_keys == []


@pytest.mark.asyncio
async def test_an_index_missing_from_storage_is_a_404_not_a_500(
    client: AsyncClient, storage: FakeStorage, monkeypatch: pytest.MonkeyPatch
) -> None:
    storage.objects.clear()
    job = make_job()
    serve(monkeypatch, job)

    response = await client.get(f"/jobs/{job.id}/preview.vtt")

    assert response.status_code == 404
