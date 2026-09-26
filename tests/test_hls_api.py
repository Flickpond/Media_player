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
from app.services.storage import get_storage_service
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


MASTER_BODY = b"#EXTM3U\n#EXT-X-STREAM-INF:BANDWIDTH=900000\nv0/index.m3u8\n"
VARIANT_BODY = b"#EXTM3U\n#EXTINF:6.0,\nseg00000.ts\n"


class FakeStorage:
    def __init__(self) -> None:
        self.objects = {
            "outputs/abc/hls/master.m3u8": MASTER_BODY,
            "outputs/abc/hls/v0/index.m3u8": VARIANT_BODY,
        }
        self.reads: list[str] = []

    async def read_object(self, object_key: str) -> bytes:
        self.reads.append(object_key)
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
async def test_the_master_playlist_is_served_not_redirected(
    client: AsyncClient, signer: FakeSigner, monkeypatch: pytest.MonkeyPatch
):
    """The regression test for the bug this route shipped with.

    A player resolves a playlist's relative URIs against the URL it finally
    fetched it from. A 307 here moved that base onto the object store, so
    every rendition resolved to an unsigned object URL and got 403 -- in a
    real browser, while every curl check passed. The body has to come from
    this route so the base stays here.
    """
    job = make_job()
    serve(monkeypatch, job)

    response = await client.get(f"/jobs/{job.id}/hls/master.m3u8", follow_redirects=False)

    assert response.status_code == 200
    assert response.content == MASTER_BODY
    assert response.headers["content-type"] == "application/vnd.apple.mpegurl"
    assert signer.inline_keys == [], "a playlist must never be handed off by redirect"


@pytest.mark.asyncio
async def test_a_relative_uri_in_a_served_playlist_resolves_back_to_this_route(
    client: AsyncClient, monkeypatch: pytest.MonkeyPatch
):
    """What a player actually does next, done here with the same URL rules.

    Resolve the playlist's first entry against the URL the playlist came
    from; the result has to land on this route again, where the owner check
    and the signing happen -- not on the object store.
    """
    from urllib.parse import urljoin

    job = make_job()
    serve(monkeypatch, job)
    master_url = f"http://test/jobs/{job.id}/hls/master.m3u8"

    response = await client.get(master_url, follow_redirects=False)
    first_entry = response.text.strip().splitlines()[-1]

    assert urljoin(str(response.url), first_entry) == f"http://test/jobs/{job.id}/hls/v0/index.m3u8"


@pytest.mark.asyncio
async def test_a_variant_playlist_is_served_too(
    client: AsyncClient, monkeypatch: pytest.MonkeyPatch
):
    job = make_job()
    serve(monkeypatch, job)

    response = await client.get(f"/jobs/{job.id}/hls/v0/index.m3u8", follow_redirects=False)

    assert response.status_code == 200
    assert response.content == VARIANT_BODY


@pytest.mark.asyncio
async def test_playlists_are_not_cacheable_by_shared_caches(
    client: AsyncClient, monkeypatch: pytest.MonkeyPatch
):
    """The body is per-owner. A shared cache keyed on the path alone would
    hand one user's playlist to the next caller of the same URL.
    """
    job = make_job()
    serve(monkeypatch, job)

    response = await client.get(f"/jobs/{job.id}/hls/master.m3u8", follow_redirects=False)

    assert "no-store" in response.headers["cache-control"]
    assert "private" in response.headers["cache-control"]


@pytest.mark.asyncio
async def test_a_playlist_missing_from_storage_is_404(
    client: AsyncClient, monkeypatch: pytest.MonkeyPatch
):
    job = make_job()
    serve(monkeypatch, job)

    response = await client.get(f"/jobs/{job.id}/hls/v7/index.m3u8", follow_redirects=False)

    assert response.status_code == 404


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

    await client.get(f"/jobs/{job.id}/hls/v0/seg00000.ts", follow_redirects=False)

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
    client: AsyncClient,
    signer: FakeSigner,
    storage: FakeStorage,
    monkeypatch: pytest.MonkeyPatch,
    path: str,
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
    assert storage.reads == []


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
