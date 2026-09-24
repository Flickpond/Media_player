"""Serving an HLS ladder without giving away the bucket.

Every other output in this application reaches the browser as one presigned
URL, which works because there is exactly one object per job. A ladder is a
master playlist, a playlist per rendition and a segment every few seconds --
hundreds of objects, and a player cannot ask for each to be signed before it
starts.

Making the prefix public-read would solve that in an afternoon and would
re-create P1, the finding sprint 1's review existed to close: anyone who
guessed a URL would get anyone's video. So each part is signed on demand,
behind the same ownership check as every other job route, and handed to the
player as a redirect.

**Why no playlist rewriting.** FFmpeg writes relative segment names, and the
variant playlists are referenced relatively from the master. A player
resolving `seg00001.ts` against `/api/jobs/{id}/hls/v0/index.m3u8` asks this
same route for `/api/jobs/{id}/hls/v0/seg00001.ts`. Serving the ladder under
a path that mirrors its storage layout is what makes the whole thing work
with no manifest post-processing at all.
"""

import re
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends
from fastapi.responses import RedirectResponse
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import CurrentUser
from app.database import get_session
from app.errors import ApiNotFoundError
from app.repositories.jobs import get_job
from app.services.output_urls import OutputUrlSigner, get_output_url_signer

router = APIRouter(prefix="/jobs", tags=["hls"])

SessionDependency = Annotated[AsyncSession, Depends(get_session)]
SignerDependency = Annotated[OutputUrlSigner, Depends(get_output_url_signer)]

# What FFmpeg actually writes: `master.m3u8` at the root, and `index.m3u8`
# plus `seg00001.ts` under a `v0`, `v1`, ... directory per rendition.
# Anchored and fully matched, so this is an allowlist of shapes rather than
# a blocklist of tricks -- `..` is not rejected by name, it simply is not a
# thing this pattern can describe.
_HLS_PATH = re.compile(r"^(?:v[0-9]{1,2}/)?[A-Za-z0-9_-]+\.(?:m3u8|ts)$")


def safe_hls_path(path: str) -> str:
    """Reduce a caller-supplied path to one of the shapes a ladder contains.

    `path` arrives from the URL and is interpolated into an object key, which
    is the same position the upload filename was in when sprint 1's review
    found it unsanitised. Object keys being opaque to S3 does not make a
    traversal harmless: `../` segments here would address another job's
    objects under the same prefix root, and this route signs whatever key it
    is given.

    Refusals are 404 rather than 400: a 400 would confirm the job exists and
    only the path was wrong, which is exactly the probe this is guarding.
    """
    if not _HLS_PATH.fullmatch(path):
        raise ApiNotFoundError("not found")
    return path


@router.get(
    "/{job_id}/hls/{path:path}",
    response_class=RedirectResponse,
    status_code=307,
)
async def get_hls_part(
    job_id: UUID,
    path: str,
    user: CurrentUser,
    session: SessionDependency,
    signer: SignerDependency,
) -> RedirectResponse:
    """Redirect to a freshly signed URL for one part of a job's ladder.

    A redirect rather than proxying the bytes: segment traffic stays between
    the browser and the object store, so the API never becomes the
    bottleneck for playback. hls.js follows 307s transparently.
    """
    # Another owner's job is 404, never 403 -- the same rule as every other
    # job route, for the same reason.
    job = await get_job(session, job_id, owner_id=user.id)
    if job is None or job.hls_key is None:
        raise ApiNotFoundError("not found")

    safe = safe_hls_path(path)

    # Derived from the job's own stored key rather than rebuilt from the
    # prefix convention, so the route cannot drift from wherever the worker
    # actually wrote the ladder.
    prefix = job.hls_key.rsplit("/", 1)[0]
    url = await signer.create_inline_url(f"{prefix}/{safe}")
    return RedirectResponse(url, status_code=307)
