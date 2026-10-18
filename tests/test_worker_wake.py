"""Waking the spot pool when a job is queued and it has no worker."""

from uuid import uuid4

import fakeredis
import pytest
from rq import Queue

from app.config import get_settings
from app.queue import enqueue_job
from app.services import worker_wake
from app.services.worker_wake import WAKE_KEY, wake_workers


class FakeAutoscaling:
    def __init__(self, desired=0, fails=None):
        self.desired = desired
        self.fails = fails
        self.set_calls = []
        self.describe_calls = 0

    def describe_auto_scaling_groups(self, AutoScalingGroupNames):
        self.describe_calls += 1
        if self.fails:
            raise self.fails
        return {"AutoScalingGroups": [{"AutoScalingGroupName": AutoScalingGroupNames[0],
                                       "DesiredCapacity": self.desired}]}

    def set_desired_capacity(self, **kwargs):
        self.set_calls.append(kwargs)


@pytest.fixture
def group(monkeypatch):
    monkeypatch.setenv("WORKER_GROUP_NAME", "flickpond-staging-workers")
    get_settings.cache_clear()
    yield "flickpond-staging-workers"
    get_settings.cache_clear()


@pytest.fixture
def redis():
    return fakeredis.FakeRedis()


def test_an_empty_pool_is_asked_for_exactly_one_worker_without_waiting_for_cooldown(group, redis):
    aws = FakeAutoscaling(desired=0)

    assert wake_workers(redis, client=aws) is True
    assert aws.set_calls == [
        {"AutoScalingGroupName": group, "DesiredCapacity": 1, "HonorCooldown": False}
    ]


def test_a_pool_with_a_worker_is_left_to_the_scaling_policy(group, redis):
    aws = FakeAutoscaling(desired=2)

    assert wake_workers(redis, client=aws) is False
    assert aws.set_calls == []


def test_a_burst_of_uploads_asks_aws_once_a_minute(group, redis):
    aws = FakeAutoscaling(desired=0)

    for _ in range(5):
        wake_workers(redis, client=aws)

    assert aws.describe_calls == 1 and len(aws.set_calls) == 1
    assert 0 < redis.ttl(WAKE_KEY) <= worker_wake.WAKE_EVERY_SECONDS


def test_no_group_configured_means_nothing_is_asked(redis, monkeypatch):
    monkeypatch.delenv("WORKER_GROUP_NAME", raising=False)
    get_settings.cache_clear()
    aws = FakeAutoscaling(desired=0)

    assert wake_workers(redis, client=aws) is False
    assert aws.describe_calls == 0 and redis.get(WAKE_KEY) is None


def test_an_aws_failure_is_logged_never_raised(group, redis, caplog):
    aws = FakeAutoscaling(fails=RuntimeError("AccessDenied"))

    assert wake_workers(redis, client=aws) is False
    assert "the scale-out alarm will" in caplog.text


def test_a_redis_failure_is_logged_never_raised(group):
    class BrokenRedis:
        def set(self, *args, **kwargs):
            raise ConnectionError("redis down")

    assert wake_workers(BrokenRedis(), client=FakeAutoscaling()) is False


def test_queueing_a_job_wakes_an_empty_pool(group, redis, monkeypatch):
    aws = FakeAutoscaling(desired=0)
    monkeypatch.setattr(worker_wake.boto3, "client", lambda *a, **k: aws)
    queue = Queue("video_jobs", connection=redis)

    job_id = uuid4()
    assert enqueue_job(job_id, queue=queue) == str(job_id)

    assert len(aws.set_calls) == 1
    assert queue.count == 1
