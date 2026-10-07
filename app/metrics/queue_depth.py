"""Publish queue depth to CloudWatch: `python -m app.metrics.queue_depth`.

The signal sprint 5's worker Auto Scaling Group scales on. It runs once, on
the core host next to Redis, and needs only Redis and permission to call
`cloudwatch:PutMetricData` -- no database and no storage. Workers on other
hosts never run it: one publisher per queue, or the datapoints double up.

    --dry-run   print what would be sent; no AWS call, no credentials
    --once      one pass, then exit non-zero if it failed
"""

import argparse
import json
import logging
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, Protocol

import boto3
from botocore.config import Config
from redis import Redis
from rq import Queue
from rq.registry import StartedJobRegistry

from app.config import get_settings
from app.queue import get_ladder_queue, get_queue, get_redis_connection
from app.worker.__main__ import configure_logging

logger = logging.getLogger("app.metrics.queue_depth")

QUEUED_METRIC = "QueuedJobs"
RUNNING_METRIC = "RunningJobs"


@dataclass(frozen=True)
class QueueDepth:
    queue: str
    queued: int
    running: int


class MetricsClient(Protocol):
    def put_metric_data(self, *, Namespace: str, MetricData: list[dict[str, Any]]) -> Any: ...


def _running(queue: Queue, now: float) -> int:
    # Read-only on purpose. StartedJobRegistry.count runs cleanup(), which
    # moves expired entries to the failed registry, and a metrics reader must
    # not change queue state. An entry's score is its expiry time, so a job
    # whose worker died mid-encode drops out by score instead.
    registry = StartedJobRegistry(queue=queue)
    return int(queue.connection.zcount(registry.key, f"({now}", "+inf"))


def read_depths(connection: Redis, *, now: float | None = None) -> list[QueueDepth]:
    """Waiting and running jobs on every queue a worker takes from.

    The queues come from app.queue, the module the API and the workers use,
    so a renamed or added queue cannot go unmeasured.
    """
    now = time.time() if now is None else now
    return [
        QueueDepth(queue=queue.name, queued=queue.count, running=_running(queue, now))
        for queue in (get_queue(connection), get_ladder_queue(connection))
    ]


def metric_data(depths: list[QueueDepth], *, environment: str) -> list[dict[str, Any]]:
    env = {"Name": "Environment", "Value": environment}

    def datum(name: str, value: int, *dimensions: dict[str, str]) -> dict[str, Any]:
        return {
            "MetricName": name,
            "Dimensions": [env, *dimensions],
            "Value": value,
            "Unit": "Count",
        }

    data = []
    for depth in depths:
        queue = {"Name": "Queue", "Value": depth.queue}
        data.append(datum(QUEUED_METRIC, depth.queued, queue))
        data.append(datum(RUNNING_METRIC, depth.running, queue))
    # Totals as their own series, with no Queue dimension. CloudWatch never
    # sums across dimensions by itself, so without these every scaling alarm
    # would need metric math -- and would silently ignore a queue added later.
    data.append(datum(QUEUED_METRIC, sum(d.queued for d in depths)))
    data.append(datum(RUNNING_METRIC, sum(d.running for d in depths)))
    return data


def publish_once(
    connection: Redis, client: MetricsClient, *, namespace: str, environment: str
) -> list[QueueDepth]:
    depths = read_depths(connection)
    client.put_metric_data(
        Namespace=namespace, MetricData=metric_data(depths, environment=environment)
    )
    return depths


def run(
    connection: Redis,
    client: MetricsClient,
    *,
    namespace: str,
    environment: str,
    interval_seconds: float,
    once: bool = False,
    sleep: Callable[[float], None] = time.sleep,
) -> None:
    while True:
        try:
            depths = publish_once(connection, client, namespace=namespace, environment=environment)
            logger.info(
                "queue depth published: %s",
                ", ".join(f"{d.queue} queued={d.queued} running={d.running}" for d in depths),
            )
        except Exception:
            if once:
                raise
            # Same stance as the reaper: one failed pass (Redis restarting,
            # CloudWatch throttling) must not end the process. A missed
            # datapoint is what the alarm's missing-data setting is for.
            logger.exception("queue depth publish failed; retrying")
        if once:
            return
        sleep(interval_seconds)


class PrintingClient:
    """--dry-run: the exact request, as JSON on stdout, with no AWS call."""

    def put_metric_data(self, *, Namespace: str, MetricData: list[dict[str, Any]]) -> None:
        print(json.dumps({"Namespace": Namespace, "MetricData": MetricData}), flush=True)


def cloudwatch_client(region: str) -> MetricsClient:
    # Credentials come from boto3's default chain: on EC2, the instance role
    # through IMDSv2. Short timeouts and few retries, because a pass that
    # hangs for minutes is worse than one that fails and runs again in 60s.
    return boto3.client(
        "cloudwatch",
        region_name=region,
        config=Config(
            connect_timeout=5,
            read_timeout=10,
            retries={"max_attempts": 3, "mode": "standard"},
        ),
    )


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description="Publish queue depth to CloudWatch")
    parser.add_argument("--once", action="store_true", help="publish one datapoint and exit")
    parser.add_argument(
        "--dry-run", action="store_true", help="print the request instead of sending it"
    )
    args = parser.parse_args(argv)

    configure_logging("INFO")
    settings = get_settings()
    if args.dry_run:
        client: MetricsClient = PrintingClient()
    elif not settings.metrics_region:
        parser.error("METRICS_REGION is not set; it must be the region the scaling alarms use")
    else:
        client = cloudwatch_client(settings.metrics_region)

    logger.info(
        "queue depth publisher starting (namespace=%s, environment=%s, every %ss%s)",
        settings.metrics_namespace,
        settings.metrics_environment,
        settings.metrics_interval_seconds,
        ", dry run" if args.dry_run else "",
    )
    run(
        get_redis_connection(),
        client,
        namespace=settings.metrics_namespace,
        environment=settings.metrics_environment,
        interval_seconds=settings.metrics_interval_seconds,
        once=args.once,
    )


if __name__ == "__main__":
    main()
