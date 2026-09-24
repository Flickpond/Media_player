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
    assert "scale=-2:min(720\\,ih)" in commands[0]


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


# --- the HLS ladder is additive: it must never cost the MP4 ---------------


class FakeLadder:
    def __init__(self, *, hls_key: str = "outputs/x/hls/master.m3u8", raises=None) -> None:
        self.hls_key = hls_key
        self.raises = raises
        self.calls: list[Path] = []

    def build(self, *, job_id, source_path, probe):
        self.calls.append(source_path)
        if self.raises is not None:
            raise self.raises
        return self.hls_key


def _mp4_writing_runner(commands: list):
    def runner(command, **_kwargs):
        commands.append(command)
        Path(command[-1]).write_bytes(b"mp4")
        return SimpleNamespace(returncode=0, stderr="")

    return runner


def test_a_successful_ladder_is_returned_alongside_the_mp4():
    store = FakeStore()
    ladder = FakeLadder(hls_key="outputs/abc/hls/master.m3u8")
    processor = FfmpegProcessor(
        store,
        output_prefix="outputs",
        runner=_mp4_writing_runner([]),
        ladder=ladder,
        prober=lambda path: SourceProbe(width=1920, height=1080, duration_seconds=10.0),
    )

    result = processor.run(job_id=uuid4(), source_key="uploads/demo.mp4")

    assert result.hls_key == "outputs/abc/hls/master.m3u8"
    # Built from the already-downloaded file rather than a second fetch.
    assert ladder.calls and ladder.calls[0].name == "source"


def test_a_failing_ladder_leaves_the_mp4_and_reports_no_ladder():
    """The contract HLS is built on: it is additive. By the time the ladder
    runs the MP4 is uploaded and the job is going to reach `done`, so a
    broken ladder costs a quality selector, never the video.
    """
    store = FakeStore()
    processor = FfmpegProcessor(
        store,
        output_prefix="outputs",
        runner=_mp4_writing_runner([]),
        ladder=FakeLadder(raises=ObjectStoreError("ffmpeg blew up", user_message="unusable")),
        prober=lambda path: SourceProbe(width=1920, height=1080, duration_seconds=10.0),
    )
    job_id = uuid4()

    result = processor.run(job_id=job_id, source_key="uploads/demo.mp4")

    assert result.output_key == f"outputs/{job_id}/demo.mp4"
    assert result.hls_key is None
    assert store.uploads[0][0] == result.output_key


def test_a_failing_probe_also_only_costs_the_ladder():
    store = FakeStore()

    def exploding_prober(_path):
        raise ObjectStoreError("ffprobe failed", user_message="unusable")

    processor = FfmpegProcessor(
        store,
        output_prefix="outputs",
        runner=_mp4_writing_runner([]),
        ladder=FakeLadder(),
        prober=exploding_prober,
    )

    result = processor.run(job_id=uuid4(), source_key="uploads/demo.mp4")

    assert result.hls_key is None
    assert result.output_key.endswith("demo.mp4")


def test_no_ladder_injected_means_no_ladder_built():
    """The pre-HLS behaviour, still reachable -- CopyProcessor and every
    existing test rely on a processor that produces only an MP4.
    """
    processor = FfmpegProcessor(
        FakeStore(), output_prefix="outputs", runner=_mp4_writing_runner([])
    )

    result = processor.run(job_id=uuid4(), source_key="uploads/demo.mp4")

    assert result.hls_key is None
