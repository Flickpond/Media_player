from datetime import UTC, datetime
from uuid import UUID, uuid4

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient

from app.api import hls as hls_api
from app.api.hls import safe_hls_path
from app.database import get_session
from app.errors import ApiNotFoundError
from app.main import create_app
from app.models.job import Job, JobStatus
from app.services.output_urls import get_output_url_signer
from tests.conftest import authenticate_as

LADDER_KEY = "outputs/abc/hls/master.m3u8"


class FakeSigner:
    """Records which flavour of URL was asked for.

    The distinction matters: the MP4 download path forces
    `Content-Disposition: attachment`, and HLS parts must not, so the route
    asking for the wrong one is a real (if quiet) bug.
    """

    def __init__(self) -> None:
        self.inline_keys: list[str] = []

    async def create_url(self, output_key: str) -> str:
        return f"https://media.example.test/{output_key}?disposition=attachment"

    async def create_inline_url(self, output_key: str) -> str:
        self.inline_keys.append(output_key)
        return f"https://media.example.test/{output_key}?signed=true"


@pytest_asyncio.fixture
async def signer():
    return FakeSigner()


@pytest_asyncio.fixture
async def client(test_user, signer: FakeSigner):
    application = create_app()

    async def fake_session():
        yield object()

    application.dependency_overrides[get_session] = fake_session
    application.dependency_overrides[get_output_url_signer] = lambda: signer
    authenticate_as(application, test_user)
    transport = ASGITransport(app=application)
    async with AsyncClient(transport=transport, base_url="http://test") as test_client:
        yield test_client


def make_job(*, hls_key: str | None = LADDER_KEY) -> Job:
    now = datetime.now(UTC)
    return Job(
        id=uuid4(),
        filename="demo.mp4",
        status=JobStatus.DONE.value,
        source_key="uploads/demo.mp4",
        output_key="outputs/abc/demo.mp4",
        hls_key=hls_key,
        created_at=now,
        updated_at=now,
    )


def serve(monkeypatch: pytest.MonkeyPatch, job: Job | None) -> None:
    async def fake_get_job(_session, _job_id: UUID, *, owner_id=None):
        return job

    monkeypatch.setattr(hls_api, "get_job", fake_get_job)


# --- the happy path -------------------------------------------------------


@pytest.mark.asyncio
async def test_a_master_playlist_redirects_to_a_signed_url(
    client: AsyncClient, signer: FakeSigner, monkeypatch: pytest.MonkeyPatch
):
    job = make_job()
    serve(monkeypatch, job)

    response = await client.get(f"/jobs/{job.id}/hls/master.m3u8", follow_redirects=False)

    assert response.status_code == 307
    assert response.headers["location"].startswith("https://media.example.test/")
    assert signer.inline_keys == ["outputs/abc/hls/master.m3u8"]


@pytest.mark.asyncio
async def test_a_variant_segment_resolves_under_the_same_prefix(
    client: AsyncClient, signer: FakeSigner, monkeypatch: pytest.MonkeyPatch
):
    """The reason no manifest rewriting is needed: a player resolving a
    relative segment name against the variant playlist's URL lands back on
    this route, one directory deeper.
    """
    job = make_job()
    serve(monkeypatch, job)

    response = await client.get(f"/jobs/{job.id}/hls/v1/seg00042.ts", follow_redirects=False)

    assert response.status_code == 307
    assert signer.inline_keys == ["outputs/abc/hls/v1/seg00042.ts"]


@pytest.mark.asyncio
async def test_hls_parts_are_signed_inline_not_as_attachments(
    client: AsyncClient, signer: FakeSigner, monkeypatch: pytest.MonkeyPatch
):
    job = make_job()
    serve(monkeypatch, job)

    await client.get(f"/jobs/{job.id}/hls/v0/index.m3u8", follow_redirects=False)

    assert signer.inline_keys, "the route used the download signer instead of the inline one"


# --- who may not have it --------------------------------------------------


@pytest.mark.asyncio
async def test_another_owners_ladder_is_404(
    client: AsyncClient, monkeypatch: pytest.MonkeyPatch
):
    """`get_job` returns None for a job this caller does not own, and the
    route must not distinguish that from an unknown id -- a 403 would
    confirm the id exists.
    """
    serve(monkeypatch, None)

    response = await client.get(f"/jobs/{uuid4()}/hls/master.m3u8", follow_redirects=False)

    assert response.status_code == 404
    assert response.json() == {"error": "not found"}


@pytest.mark.asyncio
async def test_a_job_with_no_ladder_is_404_rather_than_a_url_to_nothing(
    client: AsyncClient, signer: FakeSigner, monkeypatch: pytest.MonkeyPatch
):
    """Every job made before this feature has `hls_key IS NULL`, and so does
    any job whose ladder failed. Signing a key derived from nothing would
    hand back a URL that 404s at the object store instead.
    """
    serve(monkeypatch, make_job(hls_key=None))

    response = await client.get(f"/jobs/{uuid4()}/hls/master.m3u8", follow_redirects=False)

    assert response.status_code == 404
    assert signer.inline_keys == []


# --- the security test ----------------------------------------------------


@pytest.mark.parametrize(
    "path",
    [
        "../../uploads/someone-else/private.mp4",
        "v0/../../../outputs/other/demo.mp4",
        "..%2F..%2Fuploads%2Fprivate.mp4",
        "/etc/passwd",
        "master.m3u8.exe",
        "v999999/seg.ts",
        "seg00001.mp4",
    ],
)
@pytest.mark.asyncio
async def test_a_path_outside_the_ladder_is_404_and_signs_nothing(
    client: AsyncClient, signer: FakeSigner, monkeypatch: pytest.MonkeyPatch, path: str
):
    """`path` is interpolated into an object key, which is exactly where the
    unsanitised upload filename sat when sprint 1's review found it. Keys
    being opaque to S3 does not make traversal harmless here: `../` would
    address another job's objects under the same prefix root, and this route
    signs whatever key it is handed.

    Nothing may be signed even on the refused request -- a signed URL that
    is never returned is still a signed URL.
    """
    serve(monkeypatch, make_job())

    response = await client.get(f"/jobs/{uuid4()}/hls/{path}", follow_redirects=False)

    assert response.status_code == 404
    assert signer.inline_keys == []


# --- the guard itself, not the client's URL handling ----------------------


@pytest.mark.parametrize(
    "path",
    [
        "../master.m3u8",
        "v0/../../secrets.m3u8",
        "v0/seg00001.ts/../../master.m3u8",
        "/absolute/master.m3u8",
        "..",
        "v0//seg00001.ts",
        "v0/seg 00001.ts",
        "v0/seg00001.ts?x=1",
        "",
    ],
)
def test_safe_hls_path_refuses_anything_outside_a_ladder(path: str):
    """Tested against the function, not through the client, on purpose.

    An HTTP client normalises `../` out of a URL before it is ever sent, so
    a route test cannot prove this guard works -- it proves httpx works. A
    caller writing raw bytes to a socket is under no such obligation, and
    that caller is the one this exists for.
    """
    with pytest.raises(ApiNotFoundError):
        safe_hls_path(path)


@pytest.mark.parametrize(
    "path",
    ["master.m3u8", "v0/index.m3u8", "v0/seg00001.ts", "v12/seg99999.ts"],
)
def test_safe_hls_path_allows_what_ffmpeg_actually_writes(path: str):
    assert safe_hls_path(path) == path
