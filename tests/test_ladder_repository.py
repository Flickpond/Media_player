"""The ladder's repository writes, without a database.

The Postgres suite proves these against real rows; CI skips that suite, so
the guard that matters most -- only a done job still waiting for its ladder
can be settled -- is pinned here on the SQL itself.
"""

from datetime import UTC, datetime
from types import SimpleNamespace
from uuid import uuid4

import pytest
from sqlalchemy.dialects import postgresql

from app.models.job import HlsStatus
from app.repositories.jobs import (
    list_stale_ladders,
    mark_ladder_ready,
    mark_ladder_unavailable,
)


class RecordingSession:
    def __init__(self, row=None) -> None:
        self.row = row
        self.statements: list[str] = []
        self.params: list[dict] = []
        self.committed = self.rolled_back = False

    async def execute(self, statement):
        compiled = statement.compile(dialect=postgresql.dialect())
        self.statements.append(str(compiled))
        self.params.append(compiled.params)
        row = self.row
        return SimpleNamespace(
            scalar_one_or_none=lambda: row,
            scalars=lambda: SimpleNamespace(all=lambda: [row] if row else []),
        )

    async def commit(self):
        self.committed = True

    async def rollback(self):
        self.rolled_back = True


async def test_settling_a_ladder_is_conditional_on_a_done_job_still_waiting():
    session = RecordingSession(row="job")

    await mark_ladder_ready(session, uuid4(), hls_key="outputs/j/hls/master.m3u8")

    sql, params = session.statements[0], session.params[0]
    assert "jobs.status = " in sql and "jobs.hls_status = " in sql
    assert "done" in params.values() and HlsStatus.PENDING.value in params.values()
    assert session.committed


async def test_a_ready_ladder_writes_its_key_in_the_same_statement():
    """ck_jobs_hls_ready_has_key rejects a row where the two disagree."""
    session = RecordingSession(row="job")

    await mark_ladder_ready(session, uuid4(), hls_key="outputs/j/hls/master.m3u8")

    values = session.params[0].values()
    assert HlsStatus.READY.value in values and "outputs/j/hls/master.m3u8" in values


async def test_a_job_no_longer_waiting_is_rolled_back_and_reported_as_none():
    session = RecordingSession(row=None)

    assert await mark_ladder_unavailable(session, uuid4()) is None
    assert session.rolled_back and not session.committed


async def test_an_empty_ladder_key_is_refused_before_any_sql():
    session = RecordingSession()

    with pytest.raises(ValueError):
        await mark_ladder_ready(session, uuid4(), hls_key=" ")
    assert session.statements == []


async def test_stale_ladders_are_done_jobs_pending_since_before_the_cutoff():
    session = RecordingSession(row="job")
    cutoff = datetime(2026, 10, 6, tzinfo=UTC)

    assert await list_stale_ladders(session, before=cutoff) == ["job"]

    sql, params = session.statements[0], session.params[0]
    assert "jobs.updated_at < " in sql and cutoff in params.values()
    assert "done" in params.values() and HlsStatus.PENDING.value in params.values()


def test_the_ladder_step_is_built_from_settings(monkeypatch):
    from app.worker import storage
    from app.worker.hls import HlsLadderBuilder

    storage.get_ladder_step.cache_clear()
    monkeypatch.setattr(storage, "internal_client", lambda: object())
    monkeypatch.setattr(storage, "bucket", lambda: "videos")
    try:
        step = storage.get_ladder_step()
    finally:
        storage.get_ladder_step.cache_clear()

    assert isinstance(step, HlsLadderBuilder)
