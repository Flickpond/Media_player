"""How far an MP4 encode has got: worker writes, API reads, Redis between."""

import time
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

import fakeredis
import pytest

from app.api import jobs as jobs_api
from app.models.job import JobStatus
from app.services.progress import KEY, TTL_SECONDS, get_progress, set_progress
from app.worker.chunked import ChunkedMp4
from app.worker.probe import SourceProbe
from app.worker.progress_watch import ProgressWatcher, percent_done
from app.worker.storage import FfmpegProcessor
from tests.test_media_rules_api import api, make_job, mp4_cap  # noqa: F401  (fixtures)
from tests.test_worker_chunked import FakeFfmpeg, FakeStore, coordinator, encode


@pytest.fixture
def redis():
    return fakeredis.FakeRedis()


# --- the store --------------------------------------------------------------


def test_progress_is_kept_for_an_hour_and_never_claims_done(redis):
    job = uuid4()

    set_progress(redis, job, 100.0)
    set_progress(redis, uuid4(), -5)

    assert get_progress(redis, [job]) == {job: 99}
    assert 0 < redis.ttl(KEY.format(job)) <= TTL_SECONDS


def test_jobs_with_no_progress_are_simply_absent(redis):
    reported, silent = uuid4(), uuid4()
    set_progress(redis, reported, 41.7)

    assert get_progress(redis, [reported, silent]) == {reported: 41}
    assert get_progress(redis, []) == {}


def test_a_redis_failure_costs_the_figure_not_the_request():
    class Broken:
        def set(self, *a, **k):
            raise ConnectionError("down")

        def mget(self, *a, **k):
            raise ConnectionError("down")

    set_progress(Broken(), uuid4(), 50)
    assert get_progress(Broken(), [uuid4()]) == {}


# --- reading FFmpeg ---------------------------------------------------------------


def test_the_last_out_time_against_the_duration_is_the_percentage():
    text = "frame=10\nout_time_us=2000000\nprogress=continue\nout_time_us=5000000\n"

    assert percent_done(text, 20.0) == 25.0
    assert percent_done("out_time_us=N/A\n", 20.0) is None
    assert percent_done(text, 0) is None


def test_the_watcher_reports_what_ffmpeg_has_written(tmp_path):
    progress_file = tmp_path / "progress.txt"
    reports = []
    watcher = ProgressWatcher(progress_file, 10.0, reports.append, poll_seconds=0.05)

    watcher.read_once()
    assert reports == [], "nothing written yet"

    progress_file.write_text("out_time_us=4000000\nprogress=continue\n")
    with watcher:
        deadline = time.monotonic() + 2
        while not reports and time.monotonic() < deadline:
            time.sleep(0.02)

    assert reports and reports[-1] == 40.0


def test_a_single_encode_asks_ffmpeg_for_progress_only_when_it_can_use_it(tmp_path):
    commands = []

    def runner(command, **_kwargs):
        commands.append(command)
        Path(command[-1]).write_bytes(b"mp4")
        return SimpleNamespace(returncode=0, stderr="")

    class Store(FakeStore):
        def object_exists(self, key):
            return True

    reported = []
    for progress in (lambda job_id, p: reported.append(p), None):
        FfmpegProcessor(
            Store(), output_prefix="outputs", runner=runner, progress=progress,
            prober=lambda _p: SourceProbe(1920, 1080, 30.0),
        ).run(job_id=uuid4(), source_key="uploads/a/b.mp4")

    watched, plain = commands
    assert watched[watched.index("-progress") + 2] == "-nostats"
    assert "-progress" not in plain


def test_a_chunked_encode_reports_pieces_done_up_to_ninety(redis, tmp_path):
    reported = []
    chunker = coordinator(
        FakeStore(), redis, FakeFfmpeg(), progress=lambda job_id, p: reported.append(p)
    )

    encode(chunker, tmp_path)

    assert reported == sorted(reported)
    assert reported[-1] == 90.0, "the join and upload are the last tenth"
    assert isinstance(chunker, ChunkedMp4)


# --- the API -----------------------------------------------------------------------


async def test_a_processing_job_carries_its_progress(api, redis, monkeypatch):  # noqa: F811
    api.state["job"] = make_job(status=JobStatus.PROCESSING)
    monkeypatch.setattr(jobs_api, "get_redis_connection", lambda: redis)
    set_progress(redis, api.state["job"].id, 42)

    body = (await api.get(f"/jobs/{api.state['job'].id}")).json()

    assert body["progress"] == 42


async def test_a_finished_job_has_no_progress_even_if_some_was_left(api, redis, monkeypatch):  # noqa: F811
    api.state["job"] = make_job(status=JobStatus.DONE, height=1080)
    monkeypatch.setattr(jobs_api, "get_redis_connection", lambda: redis)
    set_progress(redis, api.state["job"].id, 99)

    body = (await api.get(f"/jobs/{api.state['job'].id}")).json()

    assert body.get("progress") is None
