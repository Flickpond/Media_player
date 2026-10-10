"""Track C's media rules as the page sees them: thumbnails, edit options, early
422s for impossible edits, and the limits endpoint.
"""

from datetime import UTC, datetime
from unittest.mock import AsyncMock, Mock
from uuid import uuid4

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient

from app.api import jobs as jobs_api
from app.config import get_settings
from app.database import get_session
from app.main import create_app
from app.models.job import HlsStatus, Job, JobStatus
from app.services.output_urls import get_output_url_signer
from app.services.storage import get_storage_service
from tests.conftest import authenticate_as


class FakeSigner:
    async def create_url(self, output_key: str) -> str:
        return f"https://media.example.test/{output_key}?signed=true"


class FakeStorage:
    def __init__(self) -> None:
        self.deleted: list[str] = []

    async def delete_object(self, object_key: str) -> None:
        self.deleted.append(object_key)

    async def list_objects(self, prefix: str = ""):
        return []


def make_job(
    *,
    status: JobStatus = JobStatus.DONE,
    height: int | None = None,
    width: int | None = None,
    operations: list[dict] | None = None,
    thumbnail_key: str | None = None,
) -> Job:
    now = datetime.now(UTC)
    return Job(
        id=uuid4(),
        filename="holiday.mov",
        status=status.value,
        source_key="uploads/holiday.mov",
        output_key="outputs/abc/holiday.mp4" if status == JobStatus.DONE else None,
        operations=operations,
        hls_status=HlsStatus.UNAVAILABLE.value,
        width=width,
        height=height,
        duration_seconds=41.2 if height else None,
        thumbnail_key=thumbnail_key,
        created_at=now,
        updated_at=now,
    )


@pytest.fixture
def mp4_cap(monkeypatch):
    """Pin the MP4 height cap, whatever the environment says."""

    def _set(height: int) -> None:
        monkeypatch.setattr(get_settings(), "worker_ffmpeg_max_height", height)

    _set(720)
    return _set


@pytest_asyncio.fixture
async def api(test_user, mp4_cap, monkeypatch):
    application = create_app()
    session = AsyncMock()

    async def database():
        yield session

    storage = FakeStorage()
    application.dependency_overrides[get_session] = database
    application.dependency_overrides[get_output_url_signer] = FakeSigner
    application.dependency_overrides[get_storage_service] = lambda: storage
    authenticate_as(application, test_user)

    state = {"job": make_job()}

    async def fake_get_job(_session, _job_id, *, owner_id=None):
        return state["job"]

    created = Job(id=uuid4())
    create_edit = AsyncMock(return_value=created)
    enqueue = Mock()
    monkeypatch.setattr(jobs_api, "get_job", fake_get_job)
    monkeypatch.setattr(jobs_api, "create_edit_job", create_edit)
    monkeypatch.setattr(jobs_api, "enqueue_job", enqueue)
    remove = AsyncMock(side_effect=lambda *_args, **_kwargs: state["job"])
    monkeypatch.setattr(jobs_api, "delete_job", remove)

    transport = ASGITransport(app=application)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        client.state = state
        client.create_edit = create_edit
        client.enqueue = enqueue
        client.storage = storage
        yield client


async def post_edit(client, operations):
    return await client.post(
        f"/jobs/{client.state['job'].id}/edit", json={"operations": operations}
    )


# --- GET /jobs/{id}: thumbnail_url -----------------------------------------


async def test_a_done_job_with_a_poster_frame_has_a_signed_thumbnail_url(api):
    api.state["job"] = make_job(height=1080, thumbnail_key="outputs/abc/thumbnail.jpg")

    body = (await api.get(f"/jobs/{api.state['job'].id}")).json()

    assert body["thumbnail_url"] == (
        "https://media.example.test/outputs/abc/thumbnail.jpg?signed=true"
    )
    assert "thumbnail_key" not in body, "object keys never leave the API"


async def test_a_job_without_a_poster_frame_omits_the_field(api):
    api.state["job"] = make_job(height=1080)

    body = (await api.get(f"/jobs/{api.state['job'].id}")).json()

    assert "thumbnail_url" not in body


# --- GET /jobs/{id}: edit_options ------------------------------------------


async def test_a_4k_upload_is_offered_options_for_the_mp4_it_is_edited_from(api):
    """The original is 2160p, but edits start from the 720p MP4: downscale
    below 720p, upscale above it -- and 1080p counts as an upscale.
    """
    api.state["job"] = make_job(width=3840, height=2160)

    body = (await api.get(f"/jobs/{api.state['job'].id}")).json()

    assert body["height"] == 2160
    assert body["edit_options"] == {"downscale": [480, 360, 240], "upscale": [1080, 1440, 2160]}


async def test_raising_the_mp4_cap_moves_the_options_with_it(api, mp4_cap):
    mp4_cap(1080)
    api.state["job"] = make_job(width=3840, height=2160)

    body = (await api.get(f"/jobs/{api.state['job'].id}")).json()

    assert body["edit_options"] == {
        "downscale": [720, 480, 360, 240],
        "upscale": [1440, 2160],
    }


async def test_an_edit_job_is_offered_options_from_its_own_height(api):
    api.state["job"] = make_job(
        height=1440, operations=[{"operation": "upscale", "params": {"height": 1440}}]
    )

    body = (await api.get(f"/jobs/{api.state['job'].id}")).json()

    assert body["edit_options"]["upscale"] == [2160]


@pytest.mark.parametrize(
    "job",
    [
        make_job(height=None),  # from before sprint 4: never probed
        make_job(status=JobStatus.PROCESSING, height=1080),
    ],
)
async def test_options_appear_only_when_a_done_job_has_known_dimensions(api, job):
    api.state["job"] = job

    body = (await api.get(f"/jobs/{job.id}")).json()

    assert "edit_options" not in body


# --- POST /jobs/{id}/edit: impossible scales are a 422 at once --------------


async def test_downscale_to_the_mp4s_own_height_is_refused_at_once(api):
    api.state["job"] = make_job(width=3840, height=2160)

    response = await post_edit(api, [{"operation": "downscale", "params": {"height": 720}}])

    assert response.status_code == 422
    assert "below the video's 720p" in response.json()["error"]
    api.create_edit.assert_not_awaited()
    api.enqueue.assert_not_called()


async def test_upscale_past_the_top_rung_is_refused(api, mp4_cap):
    mp4_cap(2160)
    api.state["job"] = make_job(width=3840, height=2160)

    response = await post_edit(api, [{"operation": "upscale", "params": {"height": 2160}}])

    assert response.status_code == 422
    assert "above the video's 2160p" in response.json()["error"]


async def test_a_height_that_is_no_rung_is_refused_even_for_an_unprobed_job(api):
    api.state["job"] = make_job(height=None)

    response = await post_edit(api, [{"operation": "upscale", "params": {"height": 1001}}])

    assert response.status_code == 422
    assert "must be one of" in response.json()["error"]


async def test_mp3_with_a_scale_is_refused_before_a_worker_ever_sees_it(api):
    api.state["job"] = make_job(height=1080)

    response = await post_edit(
        api,
        [
            {"operation": "downscale", "params": {"height": 480}},
            {"operation": "convert", "params": {"format": "mp3"}},
        ],
    )

    assert response.status_code == 422
    assert response.json() == {"error": "crop and scale cannot be combined with MP3 conversion"}


async def test_a_scale_after_a_crop_is_judged_against_the_crop(api):
    api.state["job"] = make_job(width=3840, height=2160)
    crop = {"operation": "crop", "params": {"x": 0, "y": 0, "w": 640, "h": 360}}

    refused = await post_edit(api, [crop, {"operation": "downscale", "params": {"height": 480}}])
    accepted = await post_edit(api, [crop, {"operation": "upscale", "params": {"height": 720}}])

    assert refused.status_code == 422
    assert "360p" in refused.json()["error"]
    assert accepted.status_code == 202


async def test_a_valid_scale_is_accepted_and_queued_as_before(api):
    api.state["job"] = make_job(width=3840, height=2160)

    response = await post_edit(api, [{"operation": "upscale", "params": {"height": 1080}}])

    assert response.status_code == 202
    api.enqueue.assert_called_once()


async def test_an_unprobed_job_leaves_the_comparison_to_the_worker(api):
    """No stored height: a real rung is accepted here and checked against the
    downloaded file by the worker instead.
    """
    api.state["job"] = make_job(height=None)

    response = await post_edit(api, [{"operation": "downscale", "params": {"height": 480}}])

    assert response.status_code == 202


# --- DELETE /jobs/{id} --------------------------------------------------------


async def test_deleting_a_job_deletes_its_poster_frame(api):
    api.state["job"] = make_job(height=1080, thumbnail_key="outputs/abc/thumbnail.jpg")

    response = await api.delete(f"/jobs/{api.state['job'].id}")

    assert response.status_code == 204
    assert "outputs/abc/thumbnail.jpg" in api.storage.deleted


# --- GET /limits ------------------------------------------------------------


async def test_the_page_can_read_the_duration_limit_without_signing_in(monkeypatch):
    monkeypatch.setattr(get_settings(), "media_max_duration_seconds", 300)
    application = create_app()

    async with AsyncClient(
        transport=ASGITransport(app=application), base_url="http://test"
    ) as client:
        response = await client.get("/limits")

    assert response.status_code == 200
    assert response.json() == {"max_duration_seconds": 300, "max_edit_height": 2160}
