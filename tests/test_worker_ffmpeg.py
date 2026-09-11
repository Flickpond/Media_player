from types import SimpleNamespace
from uuid import uuid4

import pytest

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

    key = processor.run(job_id=job_id, source_key="uploads/demo.webm")

    assert key == f"outputs/{job_id}/demo.mp4"
    assert store.downloads[0][0] == "uploads/demo.webm"
    assert store.uploads[0][0] == key
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
