"""Start a spot worker the moment a job is queued while the pool is at zero.

The scale-out alarm needs two one-minute samples of a waiting job before it
adds a worker, about 85 s of an overnight cold start's 3 min 30 s (sprint 5
report, §3). This asks for one worker as soon as the job is queued instead.
The alarm stays as it was: it adds workers under load, and it still starts
one if this call fails, so a failure here only costs the 85 s back.

Off unless `WORKER_GROUP_NAME` is set: local stacks and the Alibaba host have
no group to wake. Only from zero to one -- how many workers a busy queue
needs stays the scaling policy's decision, not the API's.
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


def wake_workers(connection: Redis, *, client=None) -> bool:
    """Ask for one worker if the group has none. True if it asked.

    Never raises: it runs in the request that queued the job, and that
    request has already succeeded.
    """
    settings = get_settings()
    group = settings.worker_group_name
    if not group:
        return False
    try:
        if not connection.set(WAKE_KEY, "1", nx=True, ex=WAKE_EVERY_SECONDS):
            return False
        client = client or boto3.client(
            "autoscaling", region_name=settings.worker_group_region or None
        )
        found = client.describe_auto_scaling_groups(AutoScalingGroupNames=[group])
        groups = found.get("AutoScalingGroups") or []
        if not groups or groups[0].get("DesiredCapacity", 0) > 0:
            return False
        client.set_desired_capacity(
            AutoScalingGroupName=group, DesiredCapacity=1, HonorCooldown=False
        )
        logger.info("worker group %s was at zero; asked for one worker", group)
        return True
    except Exception:
        logger.warning(
            "could not wake worker group %s; the scale-out alarm will", group, exc_info=True
        )
        return False
