"""Start spot workers as soon as there is work, not after the alarm notices.

The scale-out alarm needs two one-minute samples of waiting work before it
adds a worker. Two callers ask directly instead:

* the API, when a job is queued and the pool is at zero: one worker, about
  85 s off an overnight cold start's 3 min 30 s (sprint 5 report, §3);
* a chunked MP4's coordinator, when it queues helpers: enough workers for
  them, or they arrive after a two-minute video is already done (measured
  on flickpond.com, 19 Oct: the second worker launched 3.5 min in).

Only ever up, to `at_least` and never past the group's maximum; scaling in
stays the idle alarm's. The alarm still adds workers if a call here fails, so
a failure only costs the minutes back. Off unless `WORKER_GROUP_NAME` is set:
local stacks have no group to wake.
"""

import logging

import boto3
from redis import Redis

from app.config import get_settings

logger = logging.getLogger("app.worker_wake")

# However many uploads arrive at once, AWS is asked at most once a minute: a
# worker asked for is already booting, and asking again adds nothing.
WAKE_KEY = "flickpond:worker-wake"
WAKE_EVERY_SECONDS = 60


def wake_workers(connection: Redis, *, client=None, at_least: int = 1) -> bool:
    """Ask for `at_least` workers if the group wants fewer. True if it asked.

    Never raises: callers have already done their real work (queued the job,
    queued the helpers) and this only makes it start sooner.
    """
    settings = get_settings()
    group = settings.worker_group_name
    if not group:
        return False
    try:
        # One throttle per size asked for: the API's "one" a moment ago must
        # not swallow a coordinator's "four".
        key = WAKE_KEY if at_least == 1 else f"{WAKE_KEY}:{at_least}"
        if not connection.set(key, "1", nx=True, ex=WAKE_EVERY_SECONDS):
            return False
        client = client or boto3.client(
            "autoscaling", region_name=settings.worker_group_region or None
        )
        found = client.describe_auto_scaling_groups(AutoScalingGroupNames=[group])
        groups = found.get("AutoScalingGroups") or []
        if not groups:
            return False
        desired = groups[0].get("DesiredCapacity", 0)
        target = min(at_least, groups[0].get("MaxSize", at_least))
        if desired >= target:
            return False
        client.set_desired_capacity(
            AutoScalingGroupName=group, DesiredCapacity=target, HonorCooldown=False
        )
        logger.info("worker group %s: %d wanted, asked for %d", group, desired, target)
        return True
    except Exception:
        logger.warning(
            "could not wake worker group %s; the scale-out alarm will", group, exc_info=True
        )
        return False
