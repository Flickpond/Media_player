"""Worker entrypoint: `python -m app.worker`.

Every replica runs this same command against the same queue, which is what
makes `--scale worker=2` a real scaling test rather than a configuration
exercise -- there is no per-worker state and no job affinity (N5).
"""

import argparse
import logging
import os

from rq import Queue, SimpleWorker, Worker

from app.config import get_settings
from app.queue import get_redis_connection


def configure_logging(level: str) -> None:
    logging.basicConfig(
        level=getattr(logging, level.upper(), logging.INFO),
        format="%(asctime)s %(levelname)-8s %(name)s %(message)s",
    )


def build_worker(*, burst_safe: bool = False):
    settings = get_settings()
    connection = get_redis_connection()
    queue = Queue(settings.redis_queue, connection=connection)

    # RQ's default worker forks per job. Windows has no fork, so fall back to
    # the in-process worker there. Containers run Linux, so the deployed path
    # is the forking one.
    worker_class = Worker if hasattr(os, "fork") and not burst_safe else SimpleWorker
    return worker_class([queue], connection=connection)


def main() -> None:
    parser = argparse.ArgumentParser(description="Flickpond job worker")
    parser.add_argument(
        "--burst",
        action="store_true",
        help="drain the queue and exit, instead of waiting for more work",
    )
    parser.add_argument("--log-level", default="INFO")
    args = parser.parse_args()

    configure_logging(args.log_level)
    settings = get_settings()
    logging.getLogger("app.worker").info(
        "worker starting (queue=%s, redis=%s:%s)",
        settings.redis_queue,
        settings.redis_host,
        settings.redis_port,
    )

    worker = build_worker(burst_safe=args.burst)
    worker.work(burst=args.burst)


if __name__ == "__main__":
    main()
