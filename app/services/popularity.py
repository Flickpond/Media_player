"""Encode by popularity: a video played often enough earns an AV1 rendition.

Every video gets the H.264 ladder at upload. The AV1 one costs several times
the encode, so it is spent only on videos that are actually watched: a play
is counted each time a player opens the master playlist, and the play that
reaches `AV1_POPULAR_VIEWS` queues the rendition (app.worker.renditions),
once. Best-effort like the progress figure: Redis trouble costs a count, never
the playlist.
"""

import logging
from collections.abc import Callable
from uuid import UUID

from redis import Redis

logger = logging.getLogger("app.popularity")

PLAYS_KEY = "flickpond:plays:{}"


def record_play(
    connection: Redis,
    job_id: UUID,
    *,
    threshold: int,
    enqueue: Callable[[UUID, str], object],
) -> bool:
    """Count one play. True if this play made the video popular."""
    if threshold <= 0:
        return False
    try:
        plays = connection.incr(PLAYS_KEY.format(job_id))
        if plays != threshold:
            return False
        enqueue(job_id, "av1")
        logger.info("job %s: %d plays; AV1 rendition queued", job_id, plays)
        return True
    except Exception:
        logger.warning("job %s: play not counted", job_id, exc_info=True)
        return False
