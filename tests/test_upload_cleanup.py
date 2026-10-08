from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock
from uuid import uuid4

import pytest

from app.services import upload_cleanup as cleanup


@pytest.mark.parametrize(
    "state,removed",
    [
        ("open", True),
        ("completing", True),
        ("aborting", True),
        ("ready", False),
        ("completed", False),
        ("aborted", False),
    ],
)
async def test_cleanup_rechecks_state_under_lock_before_removing_storage(
    monkeypatch, state, removed
):
    uid, owner_id = uuid4(), uuid4()
    row = SimpleNamespace(id=uid, owner_id=owner_id, state=state)
    result = Mock()
    result.all.return_value = [(uid, owner_id)]
    session = SimpleNamespace(
        execute=AsyncMock(return_value=result), commit=AsyncMock(), rollback=AsyncMock()
    )

    @asynccontextmanager
    async def factory():
        yield session

    store = SimpleNamespace(abort=AsyncMock())
    monkeypatch.setattr(cleanup, "get_upload", AsyncMock(return_value=row))
    assert await cleanup.cleanup_expired(factory, store) == int(removed)
    assert store.abort.await_count == int(removed)
    assert row.state == ("aborted" if removed else state)


async def test_cleanup_rolls_back_failure_and_continues_other_uploads(monkeypatch):
    rows = [SimpleNamespace(id=uuid4(), owner_id=uuid4(), state="open") for _ in range(2)]
    result = Mock()
    result.all.return_value = [(row.id, row.owner_id) for row in rows]
    session = SimpleNamespace(
        execute=AsyncMock(return_value=result), commit=AsyncMock(), rollback=AsyncMock()
    )

    @asynccontextmanager
    async def factory():
        yield session

    store = SimpleNamespace(abort=AsyncMock(side_effect=[RuntimeError(), None]))
    monkeypatch.setattr(cleanup, "get_upload", AsyncMock(side_effect=rows))
    assert await cleanup.cleanup_expired(factory, store) == 1
    assert session.rollback.await_count == 2
    assert rows[1].state == "aborted"
