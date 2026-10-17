"""The retired endpoint never stores or queues video bodies."""

import pytest
from httpx import ASGITransport, AsyncClient

from app.api.uploads import safe_filename
from app.main import create_app
from tests.conftest import authenticate_as


async def test_old_upload_endpoint_is_gone_and_points_to_direct_upload(test_user):
    app = create_app()
    authenticate_as(app, test_user)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="https://test") as client:
        response = await client.post("/upload", content=b"unparsed video bytes")
    assert response.status_code == 410
    assert "POST /uploads" in response.json()["error"]


@pytest.mark.parametrize("raw,expected", [
    ("../../etc/passwd", "passwd"), ("a/b/c/clip.mp4", "clip.mp4"),
    ("..", "upload.bin"), (None, "upload.bin"), (".hidden.mp4", "hidden.mp4"),
    ("my clip;rm -rf.mp4", "my_clip_rm_-rf.mp4"),
])
def test_safe_filename_never_escapes_the_reserved_job_prefix(raw, expected):
    assert safe_filename(raw) == expected


def test_long_filenames_are_bounded():
    assert len(safe_filename("a" * 500 + ".mp4")) == 100
