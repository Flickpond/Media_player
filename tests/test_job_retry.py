"""Retry HTTP behaviour; SQL races are proved separately against PostgreSQL."""

from unittest.mock import AsyncMock, Mock
from uuid import uuid4

import pytest
from httpx import ASGITransport, AsyncClient
from redis.exceptions import ConnectionError as RedisConnectionError

from app.api import jobs as jobs_api
from app.database import get_session
from app.main import create_app
from app.repositories.jobs import InvalidJobTransitionError, JobNotFoundError
from tests.conftest import authenticate_as


@pytest.fixture
def retry_app(test_user, monkeypatch):
    application = create_app()
    session = AsyncMock()

    async def database():
        yield session

    application.dependency_overrides[get_session] = database
    authenticate_as(application, test_user)
    prepare = AsyncMock()
    enqueue = Mock()
    monkeypatch.setattr(jobs_api, "prepare_retry", prepare)
    monkeypatch.setattr(jobs_api, "enqueue_job", enqueue)
    return application, session, prepare, enqueue


async def request_retry(application, job_id):
    async with AsyncClient(
        transport=ASGITransport(app=application), base_url="http://test"
    ) as client:
        return await client.post(f"/jobs/{job_id}/retry")


async def test_retry_is_owner_scoped_enqueued_once_and_committed_after_enqueue(
    retry_app, test_user
):
    application, session, prepare, enqueue = retry_app
    job_id = uuid4()
    events = []
    enqueue.side_effect = lambda _id: events.append("enqueue")
    session.commit.side_effect = lambda: events.append("commit")

    response = await request_retry(application, job_id)

    assert response.status_code == 202
    assert response.json() == {"job_id": str(job_id)}
    prepare.assert_awaited_once_with(session, job_id, owner_id=test_user.id)
    enqueue.assert_called_once_with(job_id)
    assert events == ["enqueue", "commit"]
    session.rollback.assert_not_awaited()


@pytest.mark.parametrize(
    ("failure", "code", "message"),
    [
        (JobNotFoundError("another user's private id"), 404, "not found"),
        (InvalidJobTransitionError("internal state"), 409, "only failed jobs can be retried"),
    ],
)
async def test_rejected_retry_never_enqueues_or_exposes_internal_details(
    retry_app, failure, code, message
):
    application, session, prepare, enqueue = retry_app
    prepare.side_effect = failure
    response = await request_retry(application, uuid4())
    assert response.status_code == code
    assert response.json() == {"error": message}
    enqueue.assert_not_called()
    session.commit.assert_not_awaited()


async def test_anonymous_retry_requires_login(retry_app):
    from app.api.deps import current_user

    application, _, prepare, enqueue = retry_app
    del application.dependency_overrides[current_user]
    response = await request_retry(application, uuid4())
    assert response.status_code == 401
    prepare.assert_not_awaited()
    enqueue.assert_not_called()


async def test_retry_refuses_a_malformed_job_id(retry_app):
    application, _, prepare, enqueue = retry_app
    assert (await request_retry(application, "not-a-uuid")).status_code == 422
    prepare.assert_not_awaited()
    enqueue.assert_not_called()


async def test_queue_failure_rolls_back_and_returns_a_safe_retryable_error(retry_app):
    application, session, _, enqueue = retry_app
    enqueue.side_effect = RedisConnectionError("redis://secret@internal-host:6379")
    response = await request_retry(application, uuid4())
    assert response.status_code == 503
    assert response.json() == {
        "error": "retry could not be confirmed; refresh the job and try again"
    }
    session.rollback.assert_awaited_once()
    session.commit.assert_not_awaited()
    assert "internal-host" not in response.text


async def test_a_commit_failure_is_not_reported_as_a_successful_retry(retry_app):
    application, session, _, enqueue = retry_app
    session.commit.side_effect = RuntimeError("database disconnected")
    response = await request_retry(application, uuid4())
    assert response.status_code == 503
    enqueue.assert_called_once()
    session.rollback.assert_awaited_once()
