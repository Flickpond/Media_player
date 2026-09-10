from types import SimpleNamespace
from uuid import uuid4

import pytest

from app.worker.storage import FfmpegProcessor, ObjectStoreError


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
