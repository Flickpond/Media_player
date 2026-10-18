"""How far a job's MP4 encode has got, for the page (sprint 6 plan).

In Redis, not the database: it changes every few seconds while a job runs and
means nothing once it ends. The worker writes it; the API reads it for jobs
that are `processing` and returns it as `progress`. Both sides are
best-effort -- a progress figure is never worth failing an encode or a page
load over -- and the key expires on its own an hour after the last write.
"""

import logging
from collections.abc import Iterable
from uuid import UUID

from redis import Redis

logger = logging.getLogger("app.progress")

KEY = "flickpond:progress:{}"
TTL_SECONDS = 3600


def set_progress(connection: Redis, job_id: UUID | str, percent: float) -> None:
    """Record `percent`, held to 0-99: 100 is what `done` already says."""
    try:
        connection.set(KEY.format(job_id), max(0, min(99, int(percent))), ex=TTL_SECONDS)
    except Exception:
        logger.debug("job %s: progress not recorded", job_id, exc_info=True)


def get_progress(connection: Redis, job_ids: Iterable[UUID]) -> dict[UUID, int]:
    """Progress by job id, for the ones that have any. One round trip."""
    ids = list(job_ids)
    if not ids:
        return {}
    try:
        values = connection.mget([KEY.format(job_id) for job_id in ids])
    except Exception:
        logger.warning("progress could not be read", exc_info=True)
        return {}
    found = zip(ids, values, strict=True)
    return {job_id: int(value) for job_id, value in found if value is not None}
