from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock
from uuid import uuid4

import pytest
from httpx import ASGITransport, AsyncClient

from app.api import jobs as jobs_api
from app.database import get_session
from app.main import create_app
from app.models.job import JobStatus
from tests.conftest import authenticate_as


@pytest.fixture
def edit_app(test_user, monkeypatch):
    application = create_app()
    session = AsyncMock()

    async def database():
        yield session

    application.dependency_overrides[get_session] = database
    authenticate_as(application, test_user)

    source = SimpleNamespace(
        id=uuid4(),
        owner_id=test_user.id,
        filename="holiday.mov",
        status=JobStatus.DONE.value,
        output_key="outputs/source/holiday.mp4",
    )
    created = SimpleNamespace(id=uuid4())
    get_job = AsyncMock(return_value=source)
    create_edit = AsyncMock(return_value=created)
    enqueue = Mock()
    remove = AsyncMock()
    monkeypatch.setattr(jobs_api, "get_job", get_job)
    monkeypatch.setattr(jobs_api, "create_edit_job", create_edit)
    monkeypatch.setattr(jobs_api, "enqueue_job", enqueue)
    monkeypatch.setattr(jobs_api, "delete_job", remove)
    return application, session, source, created, get_job, create_edit, enqueue, remove


async def post_edit(application, source_id, operations):
    async with AsyncClient(
        transport=ASGITransport(app=application), base_url="http://test"
    ) as client:
        return await client.post(f"/jobs/{source_id}/edit", json={"operations": operations})


async def test_edit_is_owner_scoped_created_from_the_output_and_enqueued(edit_app, test_user):
    application, session, source, created, get_job, create_edit, enqueue, _ = edit_app
    operations = [
        {"operation": "downscale", "params": {"height": 480}},
        {"operation": "convert", "params": {"format": "mkv"}},
    ]

    response = await post_edit(application, source.id, operations)

    assert response.status_code == 202
    assert response.json() == {"job_id": str(created.id)}
    get_job.assert_awaited_once_with(session, source.id, owner_id=test_user.id)
    create_edit.assert_awaited_once_with(
        session,
        job_id=create_edit.await_args.kwargs["job_id"],
        owner_id=test_user.id,
        filename="holiday-edited.mkv",
        source_key=source.output_key,
        operations=operations,
    )
    enqueue.assert_called_once_with(created.id)


async def test_edit_rejects_a_source_that_is_not_done(edit_app):
    application, _, source, _, _, create_edit, enqueue, _ = edit_app
    source.status = JobStatus.PROCESSING.value

    response = await post_edit(
        application,
        source.id,
        [{"operation": "downscale", "params": {"height": 480}}],
    )

    assert response.status_code == 409
    assert response.json() == {"error": "only completed jobs can be edited"}
    create_edit.assert_not_awaited()
    enqueue.assert_not_called()


async def test_unknown_or_unowned_source_is_404(edit_app):
    application, _, source, _, get_job, create_edit, enqueue, _ = edit_app
    get_job.return_value = None

    response = await post_edit(
        application,
        source.id,
        [{"operation": "convert", "params": {"format": "mp3"}}],
    )

    assert response.status_code == 404
    create_edit.assert_not_awaited()
    enqueue.assert_not_called()


async def test_invalid_operation_combination_is_422_before_database_work(edit_app):
    application, _, source, _, get_job, create_edit, enqueue, _ = edit_app

    response = await post_edit(
        application,
        source.id,
        [
            {"operation": "downscale", "params": {"height": 480}},
            {"operation": "upscale", "params": {"height": 1080}},
        ],
    )

    assert response.status_code == 422
    get_job.assert_not_awaited()
    create_edit.assert_not_awaited()
    enqueue.assert_not_called()


async def test_queue_failure_removes_the_unstarted_row_and_returns_503(edit_app, test_user):
    application, session, source, created, _, _, enqueue, remove = edit_app
    enqueue.side_effect = RuntimeError("redis password and host")

    response = await post_edit(
        application,
        source.id,
        [{"operation": "clip", "params": {"start": 0, "end": 1}}],
    )

    assert response.status_code == 503
    assert response.json() == {"error": "edit could not be queued; please try again"}
    remove.assert_awaited_once_with(session, created.id, owner_id=test_user.id)
    assert "redis password" not in response.text
