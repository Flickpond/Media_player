from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock
from uuid import uuid4

import pytest
from httpx import ASGITransport, AsyncClient

from app.api import multipart_uploads as api
from app.database import get_session
from app.main import create_app
from app.models.upload import UploadSession
from app.schemas.upload import PART_SIZE
from app.services.multipart_storage import get_multipart_storage
from tests.conftest import authenticate_as

MP4 = b"\x00\x00\x00\x20ftypisom\x00\x00\x02\x00isomiso2avc1mp41"


@pytest.fixture
async def setup(monkeypatch, test_user):
    rows = {}
    jobs = []
    session = SimpleNamespace(commit=AsyncMock(), rollback=AsyncMock())
    store = SimpleNamespace(
        initiate=AsyncMock(return_value="storage-id"),
        sign=AsyncMock(return_value={"1": "http://storage/part"}),
        parts=AsyncMock(return_value=[{"n": 1, "etag": '"abc"', "size": len(MP4)}]),
        head=AsyncMock(return_value={"ContentLength": len(MP4)}),
        complete=AsyncMock(),
        sniff=AsyncMock(return_value=MP4),
        abort=AsyncMock(),
    )

    async def add(_session, upload):
        rows[upload.id] = upload

    async def get(_session, upload_id, owner_id):
        row = rows.get(upload_id)
        return row if row and row.owner_id == owner_id else None

    async def create(_session, upload):
        jobs.append(upload.job_id)
        upload.state = "ready"

    monkeypatch.setattr(api, "add_upload", add)
    monkeypatch.setattr(api, "get_upload", get)
    monkeypatch.setattr(api, "create_upload_job", create)
    enqueue = Mock()
    monkeypatch.setattr(api, "enqueue_job", enqueue)
    app = create_app()
    app.dependency_overrides[get_session] = lambda: session
    app.dependency_overrides[get_multipart_storage] = lambda: store
    authenticate_as(app, test_user)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        yield SimpleNamespace(
            client=client,
            store=store,
            rows=rows,
            jobs=jobs,
            enqueue=enqueue,
            app=app,
            user=test_user,
            session=session,
        )


async def begin(s, size=None):
    size = len(MP4) if size is None else size
    response = await s.client.post(
        "/uploads",
        json={
            "filename": "../../video.mp4",
            "size": size,
            "content_type": "video/mp4",
        },
    )
    assert response.status_code == 201, response.text
    return response.json()["upload_id"]


MANIFEST = {"parts": [{"n": 1, "etag": '"abc"'}]}


async def test_large_upload_is_only_metadata_and_can_sign_parts(setup):
    s = setup
    uid = await begin(s, 1024**3)
    row = next(iter(s.rows.values()))
    assert row.part_count == 64 and row.part_size == PART_SIZE
    assert row.filename == "video.mp4"
    assert s.jobs == []
    result = await s.client.post(f"/uploads/{uid}/parts", json={"part_numbers": [1, 64]})
    assert result.status_code == 200
    assert result.json()["urls"]["1"] == "http://storage/part"


async def test_complete_is_idempotent_and_creates_only_one_job(setup):
    s = setup
    uid = await begin(s)
    s.store.head.side_effect = [None, {"ContentLength": len(MP4)}]
    first = await s.client.post(f"/uploads/{uid}/complete", json=MANIFEST)
    second = await s.client.post(f"/uploads/{uid}/complete", json=MANIFEST)
    assert first.status_code == second.status_code == 202
    assert first.json() == second.json()
    assert len(s.jobs) == 1
    s.enqueue.assert_called_once()
    s.store.complete.assert_awaited_once()


async def test_lost_complete_response_recovers_from_storage_head(setup):
    s = setup
    uid = await begin(s)
    s.store.head.side_effect = [None, {"ContentLength": len(MP4)}]
    s.store.complete.side_effect = TimeoutError()
    assert (await s.client.post(f"/uploads/{uid}/complete", json=MANIFEST)).status_code == 503
    assert not s.jobs
    assert (await s.client.post(f"/uploads/{uid}/complete", json=MANIFEST)).status_code == 202
    assert len(s.jobs) == 1
    s.store.complete.assert_awaited_once()


async def test_queue_failure_can_retry_without_creating_another_job(setup):
    s = setup
    uid = await begin(s)
    s.enqueue.side_effect = [RuntimeError("private connection detail"), None]
    response = await s.client.post(f"/uploads/{uid}/complete", json=MANIFEST)
    assert response.status_code == 503
    assert "private" not in response.text
    assert (await s.client.post(f"/uploads/{uid}/complete", json=MANIFEST)).status_code == 202
    assert len(s.jobs) == 1


@pytest.mark.parametrize(
    "bad_size,bad_head,expected",
    [
        (1, MP4, 422),
        (len(MP4), b"<html>evil</html>", 415),
    ],
)
async def test_invalid_completed_object_is_removed_without_job(setup, bad_size, bad_head, expected):
    s = setup
    uid = await begin(s)
    s.store.head.return_value = {"ContentLength": bad_size}
    s.store.sniff.return_value = bad_head
    result = await s.client.post(f"/uploads/{uid}/complete", json=MANIFEST)
    assert result.status_code == expected
    assert not s.jobs
    s.store.abort.assert_awaited_once()


@pytest.mark.parametrize(
    "parts",
    [[], [{"n": 1, "etag": "wrong", "size": len(MP4)}], [{"n": 1, "etag": '"abc"', "size": 2}]],
)
async def test_missing_or_changed_parts_do_not_complete(setup, parts):
    s = setup
    uid = await begin(s)
    s.store.parts.return_value = parts
    assert (await s.client.post(f"/uploads/{uid}/complete", json=MANIFEST)).status_code == 422
    s.store.complete.assert_not_awaited()
    assert not s.jobs


async def test_resume_and_repeated_abort(setup):
    s = setup
    uid = await begin(s)
    result = await s.client.get(f"/uploads/{uid}")
    assert result.json()["parts_done"] == MANIFEST["parts"]
    for _ in range(2):
        assert (await s.client.delete(f"/uploads/{uid}")).status_code == 204
    s.store.abort.assert_awaited_once()
    assert (
        await s.client.post(f"/uploads/{uid}/parts", json={"part_numbers": [1]})
    ).status_code == 409
    assert (await s.client.post(f"/uploads/{uid}/complete", json=MANIFEST)).status_code == 409


async def test_cleanup_failure_keeps_upload_closed_and_can_retry(setup):
    s = setup
    uid = await begin(s)
    s.store.abort.side_effect = [RuntimeError(), None]
    assert (await s.client.delete(f"/uploads/{uid}")).status_code == 503
    assert (
        await s.client.post(f"/uploads/{uid}/parts", json={"part_numbers": [1]})
    ).status_code == 409
    assert (await s.client.delete(f"/uploads/{uid}")).status_code == 204


async def test_completed_job_cannot_be_aborted(setup):
    s = setup
    uid = await begin(s)
    await s.client.post(f"/uploads/{uid}/complete", json=MANIFEST)
    assert (await s.client.delete(f"/uploads/{uid}")).status_code == 409
    s.store.abort.assert_not_awaited()


@pytest.mark.parametrize(
    "method,suffix,body",
    [
        ("GET", "", None),
        ("DELETE", "", None),
        ("POST", "/parts", {"part_numbers": [1]}),
        ("POST", "/complete", MANIFEST),
    ],
)
async def test_every_session_endpoint_hides_other_owners(setup, other_user, method, suffix, body):
    s = setup
    uid = await begin(s)
    authenticate_as(s.app, other_user)
    response = await s.client.request(method, f"/uploads/{uid}{suffix}", json=body)
    assert response.status_code == 404


async def test_anonymous_is_401_not_storage_failure(setup):
    s = setup
    s.app.dependency_overrides.pop(api.CurrentUser.__metadata__[0].dependency)
    assert (await s.client.get(f"/uploads/{uuid4()}")).status_code == 401


@pytest.mark.parametrize("numbers", [[0], [2], [True], [1, 1], ["1"]])
async def test_invalid_signing_requests_are_422(setup, numbers):
    uid = await begin(setup)
    result = await setup.client.post(f"/uploads/{uid}/parts", json={"part_numbers": numbers})
    assert result.status_code == 422
    setup.store.sign.assert_not_awaited()


async def test_expired_upload_cannot_resume_but_can_abort(setup):
    uid = await begin(setup)
    row = next(iter(setup.rows.values()))
    row.expires_at = datetime.now(UTC) - timedelta(seconds=1)
    assert (await setup.client.get(f"/uploads/{uid}")).status_code == 410
    assert (await setup.client.post(f"/uploads/{uid}/complete", json=MANIFEST)).status_code == 410
    assert (await setup.client.delete(f"/uploads/{uid}")).status_code == 204


def test_part_count_rounds_up_without_float_arithmetic():
    assert UploadSession(size=PART_SIZE + 1, part_size=PART_SIZE).part_count == 2
