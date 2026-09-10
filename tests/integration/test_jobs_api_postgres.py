import os
from uuid import uuid4

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import delete

from app.database import get_session_factory
from app.main import create_app
from app.models.job import Job
from app.repositories.jobs import create_job, mark_done, mark_processing
from tests.conftest import authenticate_as

pytestmark = pytest.mark.skipif(
    os.getenv("RUN_POSTGRES_TESTS") != "1",
    reason="set RUN_POSTGRES_TESTS=1 to run PostgreSQL integration tests",
)


@pytest.mark.asyncio
async def test_status_endpoints_read_real_postgres_data(owner) -> None:
    job_id = uuid4()
    try:
        async with get_session_factory()() as session:
            await create_job(
                session,
                owner_id=owner.id,
                job_id=job_id,
                filename="integration.mp4",
                source_key=f"uploads/{job_id}/integration.mp4",
            )

        application = create_app()
        # The endpoints need a caller now, and this one is about the database
        # round trip rather than about signing in -- tests/test_auth.py covers
        # that. The owner is the real row the job was created against, so the
        # scoping is genuinely exercised against PostgreSQL.
        authenticate_as(application, owner)
        transport = ASGITransport(app=application)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            item_response = await client.get(f"/jobs/{job_id}")
            list_response = await client.get("/jobs")

        assert item_response.status_code == 200
        assert item_response.json() == {
            "id": str(job_id),
            "filename": "integration.mp4",
            "status": "queued",
        }
        assert list_response.status_code == 200
        assert str(job_id) in {item["id"] for item in list_response.json()}

        async with get_session_factory()() as session:
            await mark_processing(session, job_id)
            await mark_done(
                session,
                job_id,
                output_key=f"outputs/{job_id}/integration.mp4",
            )

        async with AsyncClient(transport=transport, base_url="http://test") as client:
            done_response = await client.get(f"/jobs/{job_id}")

        assert done_response.status_code == 200
        output_url = done_response.json()["output_url"]
        assert f"outputs/{job_id}/integration.mp4" in output_url
        assert "X-Amz-Signature=" in output_url
    finally:
        async with get_session_factory()() as cleanup_session:
            await cleanup_session.execute(delete(Job).where(Job.id == job_id))
            await cleanup_session.commit()
