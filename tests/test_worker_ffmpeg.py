from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

import pytest

from app.worker.probe import SourceProbe
from app.worker.storage import UNPROCESSABLE_VIDEO, FfmpegProcessor, ObjectStoreError


class FakeStore:
    def __init__(self, *, exists: bool = True) -> None:
        self.exists = exists
        self.downloads: list[tuple[str, str]] = []
        self.uploads: list[tuple[str, str]] = []

    def object_exists(self, key: str) -> bool:
        return self.exists

    def download_file(self, *, key: str, destination: str) -> None:
        self.downloads.append((key, destination))

    def upload_file(self, *, key: str, source: str) -> None:
        self.uploads.append((key, source))


def test_ffmpeg_processor_transcodes_and_uploads_mp4():
    store = FakeStore()
    commands = []

    def runner(command, **_kwargs):
        from pathlib import Path

        commands.append(command)
        Path(command[-1]).write_bytes(b"mp4")
        return SimpleNamespace(returncode=0, stderr="")

    processor = FfmpegProcessor(store, output_prefix="outputs", runner=runner)
    job_id = uuid4()

    result = processor.run(job_id=job_id, source_key="uploads/demo.webm")

    assert result.output_key == f"outputs/{job_id}/demo.mp4"
    # No ladder injected, so no ladder built -- the pre-HLS behaviour.
    assert result.hls_key is None
    assert store.downloads[0][0] == "uploads/demo.webm"
    assert store.uploads[0][0] == result.output_key
    assert commands[0][0] == "ffmpeg"
    # The MP4 is the fallback, capped at 1080p; 4K comes from the ladder.
    assert "scale=-2:min(1080\\,ih)" in commands[0]


def test_ffmpeg_processor_reports_stderr_on_failure():
    store = FakeStore()

    def runner(_command, **_kwargs):
        return SimpleNamespace(returncode=1, stderr="invalid input\ncodec error")

    processor = FfmpegProcessor(store, output_prefix="outputs", runner=runner)

    with pytest.raises(ObjectStoreError, match="codec error"):
        processor.run(job_id=uuid4(), source_key="uploads/demo.mkv")


def test_ffmpeg_processor_reports_timeout():
    import subprocess

    def runner(_command, **_kwargs):
        raise subprocess.TimeoutExpired("ffmpeg", 10)

    processor = FfmpegProcessor(FakeStore(), output_prefix="outputs", runner=runner)

    with pytest.raises(ObjectStoreError, match="timed out"):
        processor.run(job_id=uuid4(), source_key="uploads/demo.mp4")


# --- P9: FFmpeg's own words are for the log --------------------------------


def test_ffmpeg_stderr_never_reaches_the_users_half():
    """stderr names the temp path it was working on and assumes you know codecs."""

    def runner(_command, **_kwargs):
        stderr = "/tmp/flickpond-transcode-ab12/source: Invalid data found (codec error)"
        return SimpleNamespace(returncode=1, stderr=stderr)

    processor = FfmpegProcessor(FakeStore(), output_prefix="outputs", runner=runner)

    with pytest.raises(ObjectStoreError) as caught:
        processor.run(job_id=uuid4(), source_key="uploads/demo.mkv")

    assert "codec error" in str(caught.value), "the log still needs FFmpeg's own words"
    assert caught.value.user_message == UNPROCESSABLE_VIDEO
    assert "/tmp/" not in caught.value.user_message


def test_the_timeout_tells_the_user_the_limit_in_minutes_and_the_log_in_seconds():
    """Same failure, two audiences: one needs to decide what to re-upload."""
    import subprocess

    def runner(_command, **_kwargs):
        raise subprocess.TimeoutExpired("ffmpeg", 10)

    processor = FfmpegProcessor(
        FakeStore(), output_prefix="outputs", runner=runner, timeout_seconds=870
    )

    with pytest.raises(ObjectStoreError) as caught:
        processor.run(job_id=uuid4(), source_key="uploads/demo.mp4")

    assert "14 minutes" in caught.value.user_message
    assert "870 seconds" in str(caught.value), "the log keeps the exact configured value"



# --- the ladder is its own job: the MP4 job only says one is due -----------


def _mp4_writing_runner(commands: list):
    def runner(command, **_kwargs):
        commands.append(command)
        Path(command[-1]).write_bytes(b"mp4")
        return SimpleNamespace(returncode=0, stderr="")

    return runner


def _probe_1080(_path):
    return SourceProbe(width=1920, height=1080, duration_seconds=10.0)


def test_a_probed_source_finishes_with_its_ladder_still_to_come():
    """The MP4 no longer waits for every rendition: one FFmpeg run, then done."""
    commands: list = []
    processor = FfmpegProcessor(
        FakeStore(),
        output_prefix="outputs",
        runner=_mp4_writing_runner(commands),
        builds_ladder=True,
        prober=_probe_1080,
    )

    result = processor.run(job_id=uuid4(), source_key="uploads/demo.mp4")

    assert result.ladder_pending is True
    assert result.hls_key is None
    assert len(commands) == 1, "only the MP4 encode runs inside this job"


def test_an_unprobed_source_gets_no_ladder_because_nothing_could_size_it():
    """The ladder job sizes its rungs from the stored height. Without one
    there is nothing to build from, so none is promised.
    """

    def exploding_prober(_path):
        raise ObjectStoreError("ffprobe failed", user_message="unusable")

    processor = FfmpegProcessor(
        FakeStore(),
        output_prefix="outputs",
        runner=_mp4_writing_runner([]),
        builds_ladder=True,
        prober=exploding_prober,
    )

    result = processor.run(job_id=uuid4(), source_key="uploads/demo.mp4")

    assert result.ladder_pending is False
    assert result.output_key.endswith("demo.mp4")


def test_a_processor_not_asked_for_ladders_promises_none():
    """The pre-HLS behaviour, still reachable for anything that only wants an MP4."""
    processor = FfmpegProcessor(
        FakeStore(), output_prefix="outputs", runner=_mp4_writing_runner([]), prober=_probe_1080
    )

    result = processor.run(job_id=uuid4(), source_key="uploads/demo.mp4")

    assert result.ladder_pending is False
    assert result.hls_key is None


# --- sprint 4: probe once, first, and report what the source is -----------


def test_the_source_is_probed_once_before_encoding_and_its_dimensions_returned():
    """Probed first so the result can be stored for the ladder job to reuse."""
    calls: list[str] = []

    def prober(path):
        calls.append("probe")
        return SourceProbe(width=3840, height=2160, duration_seconds=41.2)

    def runner(command, **_kwargs):
        calls.append("encode")
        Path(command[-1]).write_bytes(b"mp4")
        return SimpleNamespace(returncode=0, stderr="")

    processor = FfmpegProcessor(
        FakeStore(), output_prefix="outputs", runner=runner, builds_ladder=True, prober=prober
    )

    result = processor.run(job_id=uuid4(), source_key="uploads/demo.mp4")

    assert calls == ["probe", "encode"]
    assert (result.width, result.height, result.duration_seconds) == (3840, 2160, 41.2)


def test_an_unreadable_source_still_produces_the_mp4_without_dimensions():
    """Whether an unreadable source should fail the job is the validation
    step's rule. Until that lands, a probe failure costs only the
    dimensions and the ladder -- never the video.
    """

    def exploding_prober(_path):
        raise ObjectStoreError("ffprobe failed", user_message="unusable")

    processor = FfmpegProcessor(
        FakeStore(),
        output_prefix="outputs",
        runner=_mp4_writing_runner([]),
        builds_ladder=True,
        prober=exploding_prober,
    )

    result = processor.run(job_id=uuid4(), source_key="uploads/demo.mp4")

    assert result.output_key.endswith("demo.mp4")
    assert (result.width, result.height, result.duration_seconds) == (None, None, None)
