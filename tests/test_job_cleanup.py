"""Deleting a video's objects: in parallel, bounded, and with the same failure rules."""

import asyncio
from types import SimpleNamespace
from uuid import uuid4

import pytest

from app.services import job_cleanup
from app.services.job_cleanup import CONCURRENT_DELETES, delete_job_storage


class SlowStorage:
    """Each delete takes a moment, and the peak number in flight is recorded."""

    def __init__(self, listing, fail_on=()):
        self.listing = listing
        self.fail_on = set(fail_on)
        self.deleted = []
        self.in_flight = 0
        self.peak = 0

    async def list_objects(self, prefix=""):
        return [(key, None) for key in self.listing if key.startswith(prefix)]

    async def delete_object(self, key):
        self.in_flight += 1
        self.peak = max(self.peak, self.in_flight)
        await asyncio.sleep(0.001)
        self.in_flight -= 1
        if key in self.fail_on:
            raise RuntimeError(f"AccessDenied for {key}")
        self.deleted.append(key)


def ladder_job(segments=300):
    job_id = uuid4()
    listing = [f"outputs/{job_id}/hls/v{n % 6}/seg{n:05d}.ts" for n in range(segments)]
    job = SimpleNamespace(
        id=job_id, operations=None, source_key=f"uploads/{job_id}/a.mp4",
        output_key=f"outputs/{job_id}/a.mp4", thumbnail_key=f"outputs/{job_id}/thumbnail.jpg",
        hls_key=f"outputs/{job_id}/hls/master.m3u8", preview_key=None,
    )
    return job, listing


@pytest.mark.asyncio
async def test_a_ladders_hundreds_of_objects_are_deleted_in_parallel_but_bounded():
    """One at a time, deleting an account of a few 4K videos outlasted nginx's
    60 s and the page said it failed while the API was still deleting."""
    job, listing = ladder_job(300)
    storage = SlowStorage(listing)

    await delete_job_storage(storage, job)

    assert len(storage.deleted) == 300 + 3, "every segment, the source, the MP4, the frame"
    assert 1 < storage.peak <= CONCURRENT_DELETES


@pytest.mark.asyncio
async def test_a_strict_caller_still_fails_but_only_after_every_delete_was_tried():
    job, listing = ladder_job(50)
    storage = SlowStorage(listing, fail_on={listing[0]})

    with pytest.raises(RuntimeError, match="AccessDenied"):
        await delete_job_storage(storage, job)

    assert len(storage.deleted) == 50 + 3 - 1, "the rest went; a retry has only the one left"


@pytest.mark.asyncio
async def test_a_best_effort_caller_logs_a_failure_and_carries_on(caplog):
    job, listing = ladder_job(20)
    storage = SlowStorage(listing, fail_on={listing[3]})

    await delete_job_storage(storage, job, best_effort=True)

    assert len(storage.deleted) == 20 + 3 - 1
    assert "could not delete storage object" in caplog.text


def test_the_bound_leaves_room_in_the_thread_pool_for_other_requests():
    # Starlette's default thread pool has 40 workers; deletes run in it.
    assert job_cleanup.CONCURRENT_DELETES < 40
