"""The ladder state a transition implies when its caller doesn't say.

Pure logic, so it is pinned here rather than only by the Postgres suite --
CI skips that suite, and code only it reaches looks untested to the
coverage gate.
"""

import pytest

from app.models.job import HlsStatus, JobStatus
from app.repositories.jobs import _derived_hls_status


@pytest.mark.parametrize("status", list(JobStatus))
def test_a_transition_that_writes_a_ladder_key_is_ready(status):
    assert _derived_hls_status(status, "outputs/x/hls/master.m3u8") is HlsStatus.READY


@pytest.mark.parametrize("status", [JobStatus.DONE, JobStatus.FAILED])
def test_a_job_that_settles_without_a_ladder_will_never_have_one(status):
    assert _derived_hls_status(status, None) is HlsStatus.UNAVAILABLE


@pytest.mark.parametrize("status", [JobStatus.QUEUED, JobStatus.PROCESSING])
def test_a_job_still_in_flight_without_a_ladder_is_pending(status):
    assert _derived_hls_status(status, None) is HlsStatus.PENDING
