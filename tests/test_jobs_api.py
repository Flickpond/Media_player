from datetime import UTC, datetime
from uuid import UUID, uuid4

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient

from app.api import jobs as jobs_api
from app.database import get_session
from app.main import create_app
from app.models.job import Job, JobStatus
from app.services.output_urls import get_output_url_signer


class FakeSigner:
    async def create_url(self, output_key: str) -> str:
        return f"https://media.example.test/{output_key}?signed=true"


@pytest_asyncio.fixture
async def client():
    application = create_app()

    async def fake_session():
        yield object()

    application.dependency_overrides[get_session] = fake_session
    application.dependency_overrides[get_output_url_signer] = FakeSigner
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

    async def fake_get_job(_session, job_id: UUID):
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

    async def fake_get_job(_session, _job_id: UUID):
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

    async def fake_get_job(_session, _job_id: UUID):
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
    async def fake_get_job(_session, _job_id: UUID):
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

    async def fake_list_jobs(_session):
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
    async def fake_list_jobs(_session):
        return []

    monkeypatch.setattr(jobs_api, "list_jobs", fake_list_jobs)
    response = await client.get("/jobs")

    assert response.status_code == 200
    assert response.json() == []
