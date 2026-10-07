"""The queue-depth publisher sprint 5's worker autoscaling scales on.

Real RQ queues and a real RQ worker on an in-memory Redis, so "running" means
what RQ means by it. CloudWatch is a stub: either a recorder, or botocore's
Stubber, which checks the request against CloudWatch's own API model without
sending it.
"""

import json
import time
from uuid import uuid4

import fakeredis
import pytest
from botocore.stub import Stubber
from rq import SimpleWorker
from rq.registry import StartedJobRegistry

from app.config import Settings
from app.metrics import queue_depth
from app.metrics.queue_depth import (
    QUEUED_METRIC,
    RUNNING_METRIC,
    QueueDepth,
    cloudwatch_client,
    metric_data,
    publish_once,
    read_depths,
    run,
)
from app.queue import enqueue_job, enqueue_ladder, get_ladder_queue, get_queue
from app.worker import tasks


class RecordingClient:
    def __init__(self, failures: int = 0):
        self.calls: list[dict] = []
        self._failures = failures

    def put_metric_data(self, *, Namespace, MetricData):
        if self._failures:
            self._failures -= 1
            raise RuntimeError("throttled")
        self.calls.append({"Namespace": Namespace, "MetricData": MetricData})


class StopLoop(Exception):
    pass


@pytest.fixture
def connection():
    return fakeredis.FakeStrictRedis()


@pytest.fixture
def names(connection) -> tuple[str, str]:
    return get_queue(connection).name, get_ladder_queue(connection).name


def by_queue(depths: list[QueueDepth]) -> dict[str, QueueDepth]:
    return {depth.queue: depth for depth in depths}


def test_waiting_jobs_are_counted_on_each_queue_a_worker_takes_from(connection, names):
    main, ladder = names
    enqueue_job(uuid4(), queue=get_queue(connection))
    enqueue_job(uuid4(), queue=get_queue(connection))
    enqueue_ladder(uuid4(), queue=get_ladder_queue(connection))

    depths = by_queue(read_depths(connection))

    assert set(depths) == {main, ladder}
    assert (depths[main].queued, depths[ladder].queued) == (2, 1)
    assert depths[main].running == depths[ladder].running == 0


def test_a_job_a_worker_has_started_counts_as_running_not_waiting(
    connection, names, monkeypatch
):
    main, _ = names
    seen: list[QueueDepth] = []
    monkeypatch.setattr(
        tasks, "process_job", lambda job_id: seen.extend(read_depths(connection)) or "done"
    )
    queue = get_queue(connection)
    enqueue_job(uuid4(), queue=queue)
    enqueue_job(uuid4(), queue=queue)

    SimpleWorker([queue], connection=connection).work(burst=True, max_jobs=1)

    during_first_job = by_queue(seen)[main]
    assert (during_first_job.queued, during_first_job.running) == (1, 1)
    assert by_queue(read_depths(connection))[main].running == 0


def test_a_running_entry_past_its_expiry_is_not_counted_and_is_not_moved(
    connection, names, monkeypatch
):
    """A worker that died mid-job leaves an entry behind. It must stop
    counting as demand, and reading it must not clean it up -- that is RQ's
    and the reaper's job, not a metrics reader's."""
    main, _ = names
    queue = get_queue(connection)
    observed: dict[str, int] = {}

    def task(job_id):
        far_future = time.time() + 10**8
        observed["running"] = by_queue(read_depths(connection, now=far_future))[main].running
        observed["still_registered"] = StartedJobRegistry(queue=queue).get_job_count(
            cleanup=False
        )
        observed["failed"] = queue.failed_job_registry.get_job_count(cleanup=False)
        return "done"

    monkeypatch.setattr(tasks, "process_job", task)
    enqueue_job(uuid4(), queue=queue)

    SimpleWorker([queue], connection=connection).work(burst=True)

    assert observed == {"running": 0, "still_registered": 1, "failed": 0}


def test_totals_are_published_as_their_own_series_without_a_queue_dimension():
    depths = [QueueDepth("video_jobs", 3, 1), QueueDepth("video_jobs-ladder", 2, 1)]

    data = metric_data(depths, environment="staging")

    totals = {
        d["MetricName"]: d["Value"]
        for d in data
        if d["Dimensions"] == [{"Name": "Environment", "Value": "staging"}]
    }
    assert totals == {QUEUED_METRIC: 5, RUNNING_METRIC: 2}
    per_queue = [d for d in data if len(d["Dimensions"]) == 2]
    assert {(d["MetricName"], d["Dimensions"][1]["Value"], d["Value"]) for d in per_queue} == {
        (QUEUED_METRIC, "video_jobs", 3),
        (RUNNING_METRIC, "video_jobs", 1),
        (QUEUED_METRIC, "video_jobs-ladder", 2),
        (RUNNING_METRIC, "video_jobs-ladder", 1),
    }
    assert all(d["Unit"] == "Count" for d in data)


def test_one_pass_is_one_request_cloudwatch_itself_would_accept(connection, monkeypatch):
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "testing")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "testing")
    enqueue_job(uuid4(), queue=get_queue(connection))
    client = cloudwatch_client("ap-southeast-1")
    expected = {
        "Namespace": "Flickpond",
        "MetricData": metric_data(read_depths(connection), environment="staging"),
    }

    with Stubber(client) as stub:
        stub.add_response("put_metric_data", {}, expected)
        publish_once(connection, client, namespace="Flickpond", environment="staging")
        stub.assert_no_pending_responses()


def test_a_failed_pass_is_logged_and_the_next_one_still_runs(connection, caplog):
    client = RecordingClient(failures=1)
    sleeps: list[float] = []

    def sleep(seconds):
        sleeps.append(seconds)
        if len(sleeps) == 2:
            raise StopLoop

    with pytest.raises(StopLoop):
        run(
            connection,
            client,
            namespace="Flickpond",
            environment="test",
            interval_seconds=60,
            sleep=sleep,
        )

    assert len(client.calls) == 1
    assert sleeps == [60, 60]
    assert "queue depth publish failed" in caplog.text


def test_once_reports_a_failed_pass_instead_of_swallowing_it(connection):
    with pytest.raises(RuntimeError, match="throttled"):
        run(
            connection,
            RecordingClient(failures=1),
            namespace="Flickpond",
            environment="test",
            interval_seconds=60,
            once=True,
        )


def test_dry_run_prints_the_request_and_needs_no_aws_configuration(
    connection, names, monkeypatch, capsys
):
    main, _ = names
    monkeypatch.setattr(queue_depth, "get_redis_connection", lambda: connection)
    monkeypatch.setattr(
        queue_depth,
        "get_settings",
        lambda: Settings(_env_file=None, metrics_region="", metrics_environment="dev"),
    )
    enqueue_job(uuid4(), queue=get_queue(connection))

    queue_depth.main(["--once", "--dry-run"])

    request = json.loads(capsys.readouterr().out)
    assert request["Namespace"] == "Flickpond"
    assert {
        "MetricName": QUEUED_METRIC,
        "Dimensions": [
            {"Name": "Environment", "Value": "dev"},
            {"Name": "Queue", "Value": main},
        ],
        "Value": 1,
        "Unit": "Count",
    } in request["MetricData"]


def test_publishing_for_real_refuses_to_start_without_a_region(monkeypatch, capsys):
    monkeypatch.setattr(
        queue_depth, "get_settings", lambda: Settings(_env_file=None, metrics_region="")
    )

    with pytest.raises(SystemExit) as exit_info:
        queue_depth.main(["--once"])

    assert exit_info.value.code == 2
    assert "METRICS_REGION is not set" in capsys.readouterr().err
