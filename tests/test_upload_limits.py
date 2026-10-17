from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock
from uuid import uuid4

import pytest

from app.repositories.uploads import MAX_OPEN_UPLOADS, UploadLimitReachedError, add_upload


async def test_reserving_a_slot_locks_owner_before_counting_and_inserting():
    owner = SimpleNamespace(id=uuid4())
    upload = SimpleNamespace(owner_id=owner.id)
    session = SimpleNamespace(
        scalar=AsyncMock(side_effect=[owner, MAX_OPEN_UPLOADS - 1]),
        add=Mock(), commit=AsyncMock(),
    )
    await add_upload(session, upload)
    queries = [str(call.args[0]) for call in session.scalar.await_args_list]
    assert "FOR UPDATE" in queries[0] and "users" in queries[0]
    assert "count(*)" in queries[1] and "upload_sessions.state IN" in queries[1]
    session.add.assert_called_once_with(upload)
    session.commit.assert_awaited_once()


async def test_full_quota_does_not_insert_or_commit():
    session = SimpleNamespace(
        scalar=AsyncMock(side_effect=[object(), MAX_OPEN_UPLOADS]),
        add=Mock(), commit=AsyncMock(),
    )
    with pytest.raises(UploadLimitReachedError):
        await add_upload(session, SimpleNamespace(owner_id=uuid4()))
    session.add.assert_not_called()
    session.commit.assert_not_awaited()
