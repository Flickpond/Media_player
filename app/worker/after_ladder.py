"""Work that follows a finished ladder (sprint 6 plan, 2.2).

The seek-bar preview is the first: once a video's ladder is recorded, a hook
makes its thumbnail sprite. A hook registers itself here and is called with
the job's id and its MP4's key, in the ladder job's worker, after the ladder
is `ready`.

A hook can only add to a video, never cost it: whatever one raises is logged
and the next one still runs. The ladder is recorded before any hook starts,
so a slow or failing hook never holds back the quality menu.
"""

import logging
from collections.abc import Callable
from uuid import UUID

logger = logging.getLogger("app.worker.after_ladder")

AfterLadderHook = Callable[[UUID, str], None]

HOOKS: list[AfterLadderHook] = []


def register(hook: AfterLadderHook) -> AfterLadderHook:
    """Add a hook; usable as a decorator. Registration order is run order."""
    HOOKS.append(hook)
    return hook


def run(job_id: UUID, output_key: str) -> None:
    for hook in list(HOOKS):
        try:
            hook(job_id, output_key)
        except Exception:
            name = getattr(hook, "__name__", repr(hook))
            logger.exception(
                "job %s: %s failed after the ladder; the video is unaffected", job_id, name
            )
