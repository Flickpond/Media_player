"""Probe-first rejection and the poster frame, inside the MP4 processor.

The processor is A's; what is tested here is the two seams Track C plugs
into it -- `source_check` and `thumbnailer` -- and the order they run in.
"""

from functools import partial
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

import pytest

from app.worker.probe import NO_VIDEO_STREAM, SourceProbe
from app.worker.storage import FfmpegProcessor, ObjectStoreError
from app.worker.thumbnail import THUMBNAIL_CONTENT_TYPE, ThumbnailError
from app.worker.validation import validate_source

FOUR_K = SourceProbe(width=3840, height=2160, duration_seconds=41.2)


class FakeStore:
    def __init__(self, *, fail_uploads_of: str | None = None) -> None:
        self.uploads: list[tuple[str, str]] = []
        self._fail_uploads_of = fail_uploads_of

    def object_exists(self, key: str) -> bool:
        return True

    def download_file(self, *, key: str, destination: str) -> None:
        Path(destination).write_bytes(b"source")

    def upload_file(self, *, key: str, source: str, content_type: str = "video/mp4") -> None:
        if self._fail_uploads_of and key.endswith(self._fail_uploads_of):
            raise ObjectStoreError("upload failed", user_message="could not save")
        self.uploads.append((key, content_type))


def _recording(calls: list[str], *, probe=FOUR_K, encode_returncode=0, thumbnail_raises=None):
    def prober(_path):
        calls.append("probe")
        return probe

    def thumbnailer(_source, output_path, seen_probe):
        calls.append("thumbnail")
        assert seen_probe == probe, "the thumbnail is sized from the same probe"
        if thumbnail_raises is not None:
            raise thumbnail_raises
        output_path.write_bytes(b"jpeg")
        return output_path

    def runner(command, **_kwargs):
        calls.append("encode")
        if encode_returncode == 0:
            Path(command[-1]).write_bytes(b"mp4")
        return SimpleNamespace(returncode=encode_returncode, stderr="codec error")

    return prober, thumbnailer, runner


def _processor(store, *, prober, runner, thumbnailer=None, max_duration_seconds=300):
    return FfmpegProcessor(
        store,
        output_prefix="outputs",
        runner=runner,
        builds_ladder=True,
        prober=prober,
        source_check=partial(validate_source, max_duration_seconds=max_duration_seconds),
        thumbnailer=thumbnailer,
    )


# --- probe-first rejection --------------------------------------------------


def test_a_source_over_the_duration_limit_fails_before_any_encoding():
    calls: list[str] = []
    long_video = SourceProbe(width=1920, height=1080, duration_seconds=754.2)
    prober, thumbnailer, runner = _recording(calls, probe=long_video)
    store = FakeStore()

    with pytest.raises(ObjectStoreError) as caught:
        _processor(store, prober=prober, runner=runner, thumbnailer=thumbnailer).run(
            job_id=uuid4(), source_key="uploads/long.mov"
        )

    assert calls == ["probe"], "refused in seconds: no thumbnail, no encode"
    assert store.uploads == []
    assert "12:35" in caught.value.user_message
    assert "5 minutes" in caught.value.user_message


def test_with_the_rules_wired_in_an_unreadable_source_fails_with_the_probes_message():
    """No longer "encode it blind and see": a file ffprobe cannot read, or one
    with no video in it, is refused with a message the uploader can act on.
    """
    calls: list[str] = []

    def prober(_path):
        calls.append("probe")
        raise ObjectStoreError("ffprobe found no video stream", user_message=NO_VIDEO_STREAM)

    _, _, runner = _recording(calls)

    with pytest.raises(ObjectStoreError) as caught:
        _processor(FakeStore(), prober=prober, runner=runner).run(
            job_id=uuid4(), source_key="uploads/song.m4a"
        )

    assert caught.value.user_message == NO_VIDEO_STREAM
    assert calls == ["probe"]


def test_a_source_within_the_limit_is_encoded_as_before():
    calls: list[str] = []
    prober, _, runner = _recording(calls)

    result = _processor(FakeStore(), prober=prober, runner=runner).run(
        job_id=uuid4(), source_key="uploads/demo.mp4"
    )

    assert calls == ["probe", "encode"]
    assert (result.width, result.height) == (3840, 2160)
    assert result.thumbnail_key is None, "no thumbnailer, no thumbnail"


# --- the poster frame -------------------------------------------------------


def test_the_frame_is_taken_after_the_probe_and_stored_after_the_mp4():
    calls: list[str] = []
    prober, thumbnailer, runner = _recording(calls)
    store = FakeStore()
    job_id = uuid4()

    result = _processor(store, prober=prober, runner=runner, thumbnailer=thumbnailer).run(
        job_id=job_id, source_key="uploads/demo.mp4"
    )

    assert calls == ["probe", "thumbnail", "encode"]
    assert result.thumbnail_key == f"outputs/{job_id}/thumbnail.jpg"
    assert store.uploads == [
        (f"outputs/{job_id}/demo.mp4", "video/mp4"),
        (f"outputs/{job_id}/thumbnail.jpg", THUMBNAIL_CONTENT_TYPE),
    ]


def test_a_frame_that_cannot_be_taken_costs_only_the_picture():
    calls: list[str] = []
    prober, thumbnailer, runner = _recording(
        calls, thumbnail_raises=ThumbnailError("seek past the end")
    )
    store = FakeStore()

    result = _processor(store, prober=prober, runner=runner, thumbnailer=thumbnailer).run(
        job_id=uuid4(), source_key="uploads/demo.mp4"
    )

    assert result.thumbnail_key is None
    assert result.output_key.endswith("demo.mp4")
    assert [key for key, _ in store.uploads] == [result.output_key]


def test_a_frame_that_cannot_be_stored_costs_only_the_picture():
    calls: list[str] = []
    prober, thumbnailer, runner = _recording(calls)
    store = FakeStore(fail_uploads_of="thumbnail.jpg")

    result = _processor(store, prober=prober, runner=runner, thumbnailer=thumbnailer).run(
        job_id=uuid4(), source_key="uploads/demo.mp4"
    )

    assert result.thumbnail_key is None
    assert result.output_key.endswith("demo.mp4")


def test_a_failed_encode_leaves_no_picture_behind_in_storage():
    """Taken early, uploaded late: no row would ever point at it."""
    calls: list[str] = []
    prober, thumbnailer, runner = _recording(calls, encode_returncode=1)
    store = FakeStore()

    with pytest.raises(ObjectStoreError):
        _processor(store, prober=prober, runner=runner, thumbnailer=thumbnailer).run(
            job_id=uuid4(), source_key="uploads/demo.mp4"
        )

    assert calls == ["probe", "thumbnail", "encode"]
    assert store.uploads == []


def test_an_unprobed_source_gets_no_frame_when_the_rules_are_not_wired_in():
    """The legacy path, without a `source_check`: probe failure is tolerated,
    and there is then no probe to size a thumbnail from.
    """
    calls: list[str] = []

    def prober(_path):
        raise ObjectStoreError("ffprobe failed", user_message="unusable")

    _, thumbnailer, runner = _recording(calls)
    processor = FfmpegProcessor(
        FakeStore(), output_prefix="outputs", runner=runner, prober=prober, thumbnailer=thumbnailer
    )

    result = processor.run(job_id=uuid4(), source_key="uploads/demo.mp4")

    assert calls == ["encode"]
    assert result.thumbnail_key is None
