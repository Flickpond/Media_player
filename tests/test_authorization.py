"""One caller must not see another's jobs.

This is P1 — the finding that mattered most in the sprint 1 security review,
where `GET /jobs` served every job in the table with a working signed download
URL to anyone who could reach the API. These are the tests that say it cannot
happen again.
"""

from datetime import UTC, datetime
from uuid import uuid4

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient

from app.api import jobs as jobs_api
from app.database import get_session
from app.main import create_app
from app.models.job import Job, JobStatus
from app.services.output_urls import get_output_url_signer
from tests.conftest import authenticate_as


class FakeSigner:
    async def create_url(self, output_key: str) -> str:
        return f"https://media.example.test/{output_key}?signed=true"


def make_job(owner_id, *, status: JobStatus = JobStatus.QUEUED) -> Job:
    now = datetime.now(UTC)
    return Job(
        id=uuid4(),
        owner_id=owner_id,
        filename="demo.mp4",
        status=status.value,
        source_key="uploads/demo.mp4",
        output_key=None,
        error=None,
        created_at=now,
        updated_at=now,
    )


def build_client(user):
    application = create_app()

    async def fake_session():
        yield object()

    application.dependency_overrides[get_session] = fake_session
    application.dependency_overrides[get_output_url_signer] = FakeSigner
    authenticate_as(application, user)
    return application


@pytest_asyncio.fixture
async def client_for():
    """A client authenticated as whichever user the test hands it."""
    clients = []

    async def build(user):
        transport = ASGITransport(app=build_client(user))
        c = AsyncClient(transport=transport, base_url="http://test")
        clients.append(c)
        return c

    yield build
    for c in clients:
        await c.aclose()


# --- listing ---------------------------------------------------------------


@pytest.mark.asyncio
async def test_the_list_query_is_scoped_to_the_caller(client_for, test_user, monkeypatch):
    """Scoped in the query, not filtered afterwards.

    Filtering a page after fetching it silently shrinks the page, so a caller
    with 3 of the first 50 rows would see 3 results and no way to reach the
    rest. Assert the owner reaches the repository.
    """
    seen = {}

    async def fake_list_jobs(_session, *, owner_id=None, limit, offset):
        seen["owner_id"] = owner_id
        return []

    monkeypatch.setattr(jobs_api, "list_jobs", fake_list_jobs)
    client = await client_for(test_user)

    response = await client.get("/jobs")

    assert response.status_code == 200
    assert seen["owner_id"] == test_user.id


@pytest.mark.asyncio
async def test_an_unauthenticated_caller_gets_401_not_a_list(client_for, test_user):
    """The regression guard for P1 itself."""
    application = build_client(test_user)
    application.dependency_overrides.clear()

    async def fake_session():
        yield object()

    application.dependency_overrides[get_session] = fake_session
    transport = ASGITransport(app=application)
    async with AsyncClient(transport=transport, base_url="http://test") as anonymous:
        assert (await anonymous.get("/jobs")).status_code == 401
        assert (await anonymous.get(f"/jobs/{uuid4()}")).status_code == 401
        assert (await anonymous.get("/admin/jobs")).status_code == 401


# --- reading one job -------------------------------------------------------


@pytest.mark.asyncio
async def test_another_owners_job_is_404_not_403(client_for, test_user, other_user, monkeypatch):
    """404 on purpose. A 403 confirms the id exists, which turns the endpoint
    into a way to discover valid job ids.
    """

    async def fake_get_job(_session, _job_id, *, owner_id=None):
        # The repository returns None on an owner mismatch; this is what the
        # endpoint sees.
        return None

    monkeypatch.setattr(jobs_api, "get_job", fake_get_job)
    client = await client_for(test_user)

    response = await client.get(f"/jobs/{uuid4()}")

    assert response.status_code == 404
    assert response.json() == {"error": "not found"}


@pytest.mark.asyncio
async def test_the_owner_is_passed_to_the_repository(client_for, test_user, monkeypatch):
    seen = {}

    async def fake_get_job(_session, job_id, *, owner_id=None):
        seen["owner_id"] = owner_id
        return make_job(test_user.id)

    monkeypatch.setattr(jobs_api, "get_job", fake_get_job)
    client = await client_for(test_user)

    await client.get(f"/jobs/{uuid4()}")

    assert seen["owner_id"] == test_user.id


# --- the operator view -----------------------------------------------------


@pytest.mark.asyncio
async def test_a_normal_user_cannot_reach_the_operator_view(client_for, test_user):
    client = await client_for(test_user)

    response = await client.get("/admin/jobs")

    assert response.status_code == 403


@pytest.mark.asyncio
async def test_an_operator_sees_every_owner(client_for, operator, monkeypatch):
    """The unscoped query, behind a role. This is sprint 1's operator story --
    "spot stuck jobs" -- which used to be what GET /jobs did for everybody.
    """
    seen = {}

    async def fake_list_jobs(_session, *, owner_id=None, limit, offset):
        seen["owner_id"] = owner_id
        return [make_job(uuid4()), make_job(uuid4())]

    monkeypatch.setattr(jobs_api, "list_jobs", fake_list_jobs)
    client = await client_for(operator)

    response = await client.get("/admin/jobs")

    assert response.status_code == 200
    assert seen["owner_id"] is None, "the operator view must not be scoped to the operator"
    assert len(response.json()) == 2


# --- upload ----------------------------------------------------------------


@pytest.mark.asyncio
async def test_an_upload_records_its_owner(test_user, monkeypatch):
    from app.api import uploads as uploads_api

    recorded = {}

    async def fake_create_job(_session, *, job_id, owner_id, filename, source_key):
        recorded["owner_id"] = owner_id

    class FakeStorage:
        async def upload_stream(self, *_args, **_kwargs):
            return None

    monkeypatch.setattr(uploads_api, "create_job", fake_create_job)
    monkeypatch.setattr(uploads_api, "enqueue_job", lambda job_id: str(job_id))

    from tests.test_uploads import FakeUpload

    await uploads_api.upload_video(
        file=FakeUpload(10),
        user=test_user,
        storage=FakeStorage(),
        session=object(),
    )

    assert recorded["owner_id"] == test_user.id
