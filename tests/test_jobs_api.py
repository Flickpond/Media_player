from datetime import UTC, datetime
from uuid import UUID, uuid4

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient

from app.api import jobs as jobs_api
from app.database import get_session
from app.main import create_app
from app.models.job import Job, JobStatus
from app.repositories.jobs import DEFAULT_PAGE_SIZE, MAX_PAGE_SIZE
from app.services.output_urls import get_output_url_signer
from app.services.storage import get_storage_service
from tests.conftest import authenticate_as


class FakeSigner:
    async def create_url(self, output_key: str) -> str:
        return f"https://media.example.test/{output_key}?signed=true"


class FakeStorage:
    def __init__(self, *, fail_on: set[str] | None = None) -> None:
        self.deleted: list[str] = []
        self._fail_on = fail_on or set()

    async def delete_object(self, object_key: str) -> None:
        if object_key in self._fail_on:
            raise RuntimeError(f"storage unreachable for {object_key}")
        self.deleted.append(object_key)


@pytest_asyncio.fixture
async def storage():
    return FakeStorage()


@pytest_asyncio.fixture
async def client(test_user, storage: FakeStorage):
    application = create_app()

    async def fake_session():
        yield object()

    application.dependency_overrides[get_session] = fake_session
    application.dependency_overrides[get_output_url_signer] = FakeSigner
    application.dependency_overrides[get_storage_service] = lambda: storage
    authenticate_as(application, test_user)
    transport = ASGITransport(app=application)
    async with AsyncClient(transport=transport, base_url="http://test") as test_client:
        yield test_client


def make_job(
    *,
    status: JobStatus = JobStatus.QUEUED,
    output_key: str | None = None,
    error: str | None = None,
) -> Job:
    now = datetime.now(UTC)
    return Job(
        id=uuid4(),
        filename="demo.mp4",
        status=status.value,
        source_key="uploads/demo.mp4",
        output_key=output_key,
        error=error,
        created_at=now,
        updated_at=now,
    )


@pytest.mark.asyncio
async def test_health(client: AsyncClient) -> None:
    response = await client.get("/health")

    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


@pytest.mark.asyncio
async def test_get_queued_job_omits_internal_and_empty_fields(
    client: AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    job = make_job()

    async def fake_get_job(_session, job_id: UUID, *, owner_id=None):
        assert job_id == job.id
        return job

    monkeypatch.setattr(jobs_api, "get_job", fake_get_job)
    response = await client.get(f"/jobs/{job.id}")

    assert response.status_code == 200
    assert response.json() == {
        "id": str(job.id),
        "filename": "demo.mp4",
        "status": "queued",
    }


@pytest.mark.asyncio
async def test_get_done_job_returns_presigned_output_url(
    client: AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    job = make_job(status=JobStatus.DONE, output_key="outputs/demo.mp4")

    async def fake_get_job(_session, _job_id: UUID, *, owner_id=None):
        return job

    monkeypatch.setattr(jobs_api, "get_job", fake_get_job)
    response = await client.get(f"/jobs/{job.id}")

    assert response.status_code == 200
    assert response.json()["output_url"] == (
        "https://media.example.test/outputs/demo.mp4?signed=true"
    )
    assert "output_key" not in response.json()
    assert "source_key" not in response.json()


@pytest.mark.asyncio
async def test_get_failed_job_returns_readable_error(
    client: AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    job = make_job(status=JobStatus.FAILED, error="copy failed: object missing")

    async def fake_get_job(_session, _job_id: UUID, *, owner_id=None):
        return job

    monkeypatch.setattr(jobs_api, "get_job", fake_get_job)
    response = await client.get(f"/jobs/{job.id}")

    assert response.status_code == 200
    assert response.json()["error"] == "copy failed: object missing"
    assert "output_url" not in response.json()


@pytest.mark.asyncio
async def test_get_unknown_job_returns_contract_error(
    client: AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def fake_get_job(_session, _job_id: UUID, *, owner_id=None):
        return None

    monkeypatch.setattr(jobs_api, "get_job", fake_get_job)
    response = await client.get(f"/jobs/{uuid4()}")

    assert response.status_code == 404
    assert response.json() == {"error": "not found"}


@pytest.mark.asyncio
async def test_list_jobs_returns_contract_shape(
    client: AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    queued = make_job()
    done = make_job(status=JobStatus.DONE, output_key="outputs/ready.mp4")

    async def fake_list_jobs(_session, *, owner_id=None, limit, offset):
        return [done, queued]

    monkeypatch.setattr(jobs_api, "list_jobs", fake_list_jobs)
    response = await client.get("/jobs")

    assert response.status_code == 200
    payload = response.json()
    assert [item["id"] for item in payload] == [str(done.id), str(queued.id)]
    assert payload[0]["output_url"].startswith("https://media.example.test/")
    assert "output_url" not in payload[1]


@pytest.mark.asyncio
async def test_list_jobs_can_be_empty(client: AsyncClient, monkeypatch: pytest.MonkeyPatch) -> None:
    async def fake_list_jobs(_session, *, owner_id=None, limit, offset):
        return []

    monkeypatch.setattr(jobs_api, "list_jobs", fake_list_jobs)
    response = await client.get("/jobs")

    assert response.status_code == 200
    assert response.json() == []


# --- pagination (P5) ------------------------------------------------------


@pytest.fixture
def captured_page(monkeypatch: pytest.MonkeyPatch) -> dict:
    """Records the limit/offset the endpoint actually asked the repository for."""
    seen: dict = {}

    async def fake_list_jobs(_session, *, owner_id=None, limit, offset):
        seen["limit"] = limit
        seen["offset"] = offset
        return []

    monkeypatch.setattr(jobs_api, "list_jobs", fake_list_jobs)
    return seen


@pytest.mark.asyncio
async def test_a_caller_that_asks_for_nothing_gets_a_capped_page(
    client: AsyncClient, captured_page: dict
) -> None:
    """The one behaviour that changed in the review: no parameters used to mean
    the whole table, and the endpoint mints a signed URL per row returned.
    """
    response = await client.get("/jobs")

    assert response.status_code == 200
    assert captured_page == {"limit": DEFAULT_PAGE_SIZE, "offset": 0}


@pytest.mark.asyncio
async def test_limit_and_offset_reach_the_repository(
    client: AsyncClient, captured_page: dict
) -> None:
    response = await client.get("/jobs", params={"limit": 10, "offset": 40})

    assert response.status_code == 200
    assert captured_page == {"limit": 10, "offset": 40}


@pytest.mark.asyncio
async def test_the_cap_cannot_be_argued_past(client: AsyncClient, captured_page: dict) -> None:
    response = await client.get("/jobs", params={"limit": MAX_PAGE_SIZE + 1})

    assert response.status_code == 422
    assert captured_page == {}


@pytest.mark.asyncio
@pytest.mark.parametrize("params", [{"limit": 0}, {"limit": -1}, {"offset": -1}])
async def test_nonsense_paging_is_refused(
    client: AsyncClient, captured_page: dict, params: dict
) -> None:
    response = await client.get("/jobs", params=params)

    assert response.status_code == 422
    assert captured_page == {}


@pytest.mark.asyncio
async def test_the_maximum_page_size_is_allowed(client: AsyncClient, captured_page: dict) -> None:
    """Test *at* the limit, not only past it."""
    response = await client.get("/jobs", params={"limit": MAX_PAGE_SIZE})

    assert response.status_code == 200
    assert captured_page["limit"] == MAX_PAGE_SIZE


# --- DELETE /jobs/{id}: an accidental upload, or general library cleanup ---


@pytest.mark.asyncio
async def test_deleting_a_done_job_removes_both_storage_objects(
    client: AsyncClient, storage: FakeStorage, monkeypatch: pytest.MonkeyPatch
) -> None:
    job = make_job(status=JobStatus.DONE, output_key="outputs/demo.mp4")

    async def fake_delete_job(_session, job_id: UUID, *, owner_id):
        assert job_id == job.id
        return job

    monkeypatch.setattr(jobs_api, "delete_job", fake_delete_job)
    response = await client.delete(f"/jobs/{job.id}")

    assert response.status_code == 204
    assert response.content == b""
    assert sorted(storage.deleted) == ["outputs/demo.mp4", "uploads/demo.mp4"]


@pytest.mark.asyncio
async def test_deleting_a_queued_job_only_touches_the_source_key(
    client: AsyncClient, storage: FakeStorage, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A queued job has no output yet -- nothing to delete that does not exist."""
    job = make_job(status=JobStatus.QUEUED)

    async def fake_delete_job(_session, _job_id, *, owner_id):
        return job

    monkeypatch.setattr(jobs_api, "delete_job", fake_delete_job)
    response = await client.delete(f"/jobs/{job.id}")

    assert response.status_code == 204
    assert storage.deleted == ["uploads/demo.mp4"]


@pytest.mark.asyncio
async def test_deleting_an_unknown_or_unowned_job_returns_contract_error(
    client: AsyncClient, storage: FakeStorage, monkeypatch: pytest.MonkeyPatch
) -> None:
    """404, never 403 -- same reasoning as GET: do not confirm the id exists."""

    async def fake_delete_job(_session, _job_id, *, owner_id):
        return None

    monkeypatch.setattr(jobs_api, "delete_job", fake_delete_job)
    response = await client.delete(f"/jobs/{uuid4()}")

    assert response.status_code == 404
    assert response.json() == {"error": "not found"}
    assert storage.deleted == []


@pytest.mark.asyncio
async def test_a_storage_failure_does_not_undo_the_delete(
    test_user, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The row is already gone by the time storage cleanup runs (best-effort:
    an orphaned object is exactly what the reaper's sweep exists to catch),
    so a failure here must not turn into a failure response.
    """
    job = make_job(status=JobStatus.DONE, output_key="outputs/demo.mp4")
    failing_storage = FakeStorage(fail_on={"uploads/demo.mp4", "outputs/demo.mp4"})

    async def fake_delete_job(_session, _job_id, *, owner_id):
        return job

    monkeypatch.setattr(jobs_api, "delete_job", fake_delete_job)

    application = create_app()

    async def fake_session():
        yield object()

    application.dependency_overrides[get_session] = fake_session
    application.dependency_overrides[get_output_url_signer] = FakeSigner
    application.dependency_overrides[get_storage_service] = lambda: failing_storage
    authenticate_as(application, test_user)
    transport = ASGITransport(app=application)
    async with AsyncClient(transport=transport, base_url="http://test") as failing_client:
        response = await failing_client.delete(f"/jobs/{job.id}")

    assert response.status_code == 204
    assert failing_storage.deleted == []
