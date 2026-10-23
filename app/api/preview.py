"""The seek-bar preview's index, with its sheet signed for this viewer.

Sprint 6 plan, 2.2. The worker stores a sheet of small frames and a WebVTT
index whose cues each name a rectangle of it -- `sprite.jpg#xywh=0,0,160,90`
(app/worker/preview.py). The sheet is private like every output, so the
index cannot be handed out as it is stored: Plyr would resolve `sprite.jpg`
against the index's own URL and be refused.

So the index is served here, behind the same ownership check as every other
job route, with each sheet named in it swapped for a URL signed just now --
through CloudFront where the segments go, so a sheet costs what a segment
does. The cues keep their `#xywh=` part; Plyr splits on it, so a signed URL's
query string in front of it is read as the image's address, whole.
"""

import re
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends
from fastapi.responses import Response
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import CurrentUser
from app.database import get_session
from app.errors import ApiNotFoundError
from app.models.job import JobStatus
from app.repositories.jobs import get_job
from app.schemas.job import ErrorResponse
from app.services.output_urls import OutputUrlSigner, get_output_url_signer
from app.services.storage import StorageService, get_storage_service

router = APIRouter(prefix="/jobs", tags=["preview"])

SessionDependency = Annotated[AsyncSession, Depends(get_session)]
SignerDependency = Annotated[OutputUrlSigner, Depends(get_output_url_signer)]
StorageDependency = Annotated[StorageService, Depends(get_storage_service)]

VTT_CONTENT_TYPE = "text/vtt"

# A cue's text: a sheet's name, then its rectangle. Only a bare name in the
# preview's own folder is signed -- anything else in the file, which only the
# worker writes, is left as it is rather than turned into a key.
_SHEET_CUE = re.compile(r"^(?P<name>[A-Za-z0-9_-]{1,64}\.jpg)(?=#xywh=)", re.MULTILINE)


def preview_vtt_path(job_id: UUID) -> str:
    """The route below, as the job shape names it (`preview_vtt_url`)."""
    return f"/jobs/{job_id}/preview.vtt"


@router.get(
    "/{job_id}/preview.vtt",
    response_model=None,
    responses={404: {"model": ErrorResponse}},
)
async def get_preview_index(
    job_id: UUID,
    user: CurrentUser,
    session: SessionDependency,
    signer: SignerDependency,
    storage: StorageDependency,
) -> Response:
    # Another owner's job is 404, never 403, as on every job route.
    job = await get_job(session, job_id, owner_id=user.id)
    if job is None or job.status != JobStatus.DONE.value or not job.preview_key:
        raise ApiNotFoundError("not found")

    try:
        body = (await storage.read_object(job.preview_key)).decode("utf-8")
    except Exception as exc:
        # Recorded but not there: removed underneath a live row. The player
        # simply shows no preview.
        raise ApiNotFoundError("not found") from exc

    folder = job.preview_key.rsplit("/", 1)[0]
    signed = {
        name: await signer.create_inline_url(f"{folder}/{name}")
        for name in dict.fromkeys(match["name"] for match in _SHEET_CUE.finditer(body))
    }
    body = _SHEET_CUE.sub(lambda match: signed[match["name"]], body)
    # `private, no-store`: the body carries this viewer's signatures, which a
    # shared cache keyed on the path would hand to the next caller.
    return Response(
        content=body,
        media_type=VTT_CONTENT_TYPE,
        headers={"Cache-Control": "private, no-store"},
    )
