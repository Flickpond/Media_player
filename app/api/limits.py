"""What the page needs to know before it uploads anything.

The duration limit is enforced by the worker, right after the probe -- but a
page that knows it can say "up to 5 minutes" next to the picker, and check a
file's length in the browser before spending minutes uploading it. One source
of truth: the same setting the worker reads.

Public, like `/health`: nothing here is specific to a user or worth hiding,
and the sign-in page is where a newcomer first wants to read it.
"""

from fastapi import APIRouter
from pydantic import BaseModel

from app.config import get_settings
from app.services.media_rules import MAX_SCALE_HEIGHT

router = APIRouter(tags=["limits"])


class LimitsResponse(BaseModel):
    # 0 means no limit is configured.
    max_duration_seconds: int
    # The tallest an edit may scale to. The editor's per-video choices are on
    # each job as `edit_options`; this is the ceiling they are all under.
    max_edit_height: int
    # The largest file `POST /uploads` accepts. Here so the page can say
    # "up to 2 GB" and refuse a bigger file before sending any of it, rather
    # than quoting the number from a 413 after the fact.
    max_upload_bytes: int


@router.get("/limits", response_model=LimitsResponse)
async def get_limits() -> LimitsResponse:
    settings = get_settings()
    return LimitsResponse(
        max_duration_seconds=max(0, settings.media_max_duration_seconds),
        max_edit_height=MAX_SCALE_HEIGHT,
        max_upload_bytes=settings.max_upload_bytes,
    )
