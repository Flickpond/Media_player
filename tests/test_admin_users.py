from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest
from httpx import ASGITransport, AsyncClient

from app.api import admin_users as api
from app.database import get_session
from app.main import create_app
from app.services.multipart_storage import get_multipart_storage
from app.services.storage import get_storage_service
from tests.conftest import authenticate_as


@pytest.fixture
async def account_api(monkeypatch, operator, other_user):
    app = create_app()
    session = SimpleNamespace(commit=AsyncMock(), rollback=AsyncMock())
    storage = SimpleNamespace(delete_object=AsyncMock(), list_objects=AsyncMock(return_value=[]))
    multipart = SimpleNamespace(abort=AsyncMock())
    lock = AsyncMock(return_value=other_user)
    contents = AsyncMock(return_value=([], []))
    remove = AsyncMock()
    listing = AsyncMock(return_value=[{
        "id": other_user.id, "email": other_user.email, "role": "user", "job_count": 3,
    }])
    for name, mock in [("lock_owner", lock), ("account_contents", contents),
                       ("delete_account_rows", remove), ("list_users_with_job_counts", listing)]:
        monkeypatch.setattr(api, name, mock)
    app.dependency_overrides[get_session] = lambda: session
    app.dependency_overrides[get_storage_service] = lambda: storage
    app.dependency_overrides[get_multipart_storage] = lambda: multipart
    authenticate_as(app, operator)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="https://test") as client:
        yield SimpleNamespace(
            app=app, client=client, session=session, storage=storage, multipart=multipart,
            lock=lock, contents=contents, remove=remove, listing=listing,
            target=other_user, operator=operator,
        )


@pytest.mark.parametrize("method,path", [("get", ""), ("delete", "/{id}")])
async def test_account_management_requires_an_operator(account_api, test_user, method, path):
    s = account_api
    authenticate_as(s.app, test_user)
    response = await getattr(s.client, method)("/admin/users" + path.format(id=s.target.id))
    assert response.status_code == 403
    assert response.json() == {"error": "operator role required"}
    s.lock.assert_not_awaited()
    s.listing.assert_not_awaited()


async def test_operator_cannot_delete_their_own_account(account_api):
    s = account_api
    response = await s.client.delete(f"/admin/users/{s.operator.id}")
    assert response.status_code == 409
    assert response.json() == {"error": "an operator cannot delete their own account"}
    s.lock.assert_not_awaited()


async def test_unknown_account_is_404(account_api):
    s = account_api
    s.lock.return_value = None
    response = await s.client.delete(f"/admin/users/{uuid4()}")
    assert response.status_code == 404 and response.json() == {"error": "not found"}
    s.remove.assert_not_awaited()
    s.session.commit.assert_not_awaited()


async def test_user_listing_includes_video_count_and_never_password_hashes(account_api):
    s = account_api
    response = await s.client.get("/admin/users?limit=10&offset=20")
    assert response.status_code == 200
    assert response.json()[0]["job_count"] == 3
    assert "password_hash" not in response.text
    s.listing.assert_awaited_once_with(s.session, limit=10, offset=20)


async def test_storage_and_open_sessions_are_cleaned_before_rows_and_one_commit(account_api):
    s = account_api
    job = SimpleNamespace(
        id=uuid4(), source_key="source", output_key="mp4", thumbnail_key="thumb",
        hls_key="hls/id/master.m3u8", operations=None,
    )
    edit = SimpleNamespace(
        id=uuid4(), source_key="borrowed", output_key="edit", thumbnail_key=None,
        hls_key=None, operations=[{"operation": "clip"}],
    )
    upload = SimpleNamespace(state="open")
    s.contents.return_value = ([job, edit], [upload, SimpleNamespace(state="completed")])
    s.storage.list_objects.return_value = [("hls/id/master.m3u8", None), ("hls/id/seg.ts", None)]
    events = []
    s.multipart.abort.side_effect = lambda _: events.append("abort")
    s.storage.delete_object.side_effect = lambda key: events.append(key)
    s.remove.side_effect = lambda *_: events.append("rows")
    s.session.commit.side_effect = lambda: events.append("commit")
    response = await s.client.delete(f"/admin/users/{s.target.id}")
    assert response.status_code == 204 and response.content == b""
    assert events == ["abort", "source", "mp4", "thumb", "hls/id/master.m3u8",
                      "hls/id/seg.ts", "edit", "rows", "commit"]
    s.session.commit.assert_awaited_once()


@pytest.mark.parametrize("failure", ["abort", "list", "delete", "database"])
async def test_cleanup_failure_keeps_rows_and_returns_a_safe_retryable_error(account_api, failure):
    s = account_api
    job = SimpleNamespace(
        id=uuid4(), source_key="private/source", output_key=None, thumbnail_key=None,
        hls_key="hls/id/master.m3u8", operations=None,
    )
    s.contents.return_value = ([job], [SimpleNamespace(state="completing")])
    mock = {"abort": s.multipart.abort, "list": s.storage.list_objects,
            "delete": s.storage.delete_object, "database": s.session.commit}[failure]
    mock.side_effect = RuntimeError("private/source secret connection detail")
    response = await s.client.delete(f"/admin/users/{s.target.id}")
    assert response.status_code == 503
    assert "private" not in response.text and "secret" not in response.text
    if failure != "database":
        s.remove.assert_not_awaited()
        s.session.commit.assert_not_awaited()
    s.session.rollback.assert_awaited_once()
    mock.side_effect = None
    assert (await s.client.delete(f"/admin/users/{s.target.id}")).status_code == 204


async def test_deleted_users_cookie_is_rejected_on_the_next_request(account_api, monkeypatch):
    from app.api import deps
    from app.services.security import COOKIE_NAME

    s = account_api
    s.app.dependency_overrides.clear()
    s.app.dependency_overrides[get_session] = lambda: s.session
    monkeypatch.setattr(deps, "read_token", lambda _: (s.target.id, "user"))
    monkeypatch.setattr(deps, "get_user", AsyncMock(return_value=None))
    response = await s.client.get("/auth/me", headers={"Cookie": f"{COOKIE_NAME}=old-token"})
    assert response.status_code == 401
