import os
from uuid import uuid4

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import delete

from app.database import AsyncSessionFactory
from app.main import create_app
from app.models.job import Job
from app.repositories.jobs import create_job, mark_done, mark_processing

pytestmark = pytest.mark.skipif(
    os.getenv("RUN_POSTGRES_TESTS") != "1",
    reason="set RUN_POSTGRES_TESTS=1 to run PostgreSQL integration tests",
)


@pytest.mark.asyncio
async def test_status_endpoints_read_real_postgres_data() -> None:
    job_id = uuid4()
    try:
        async with AsyncSessionFactory() as session:
            await create_job(
                session,
                job_id=job_id,
                filename="integration.mp4",
                source_key=f"uploads/{job_id}/integration.mp4",
            )

        application = create_app()
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

        async with AsyncSessionFactory() as session:
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
        async with AsyncSessionFactory() as cleanup_session:
            await cleanup_session.execute(delete(Job).where(Job.id == job_id))
            await cleanup_session.commit()
