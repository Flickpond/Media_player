"""Track C's worker rules against real FFmpeg: a phone video the right way up,
a poster frame from it, and a too-long upload refused before encoding.

Gated like every integration test, and on ffmpeg/ffprobe being installed.
"""

import os
import shutil
import subprocess
from functools import partial
from pathlib import Path
from uuid import uuid4

import pytest

from app.worker.probe import probe_source
from app.worker.storage import FfmpegProcessor, ObjectStoreError
from app.worker.thumbnail import extract_thumbnail
from app.worker.validation import validate_source

pytestmark = [
    pytest.mark.skipif(
        os.getenv("RUN_POSTGRES_TESTS") != "1",
        reason="set RUN_POSTGRES_TESTS=1 to run integration tests",
    ),
    pytest.mark.skipif(
        shutil.which("ffmpeg") is None or shutil.which("ffprobe") is None,
        reason="ffmpeg and ffprobe are required",
    ),
]


def _ffmpeg(*args: str) -> None:
    subprocess.run(
        ["ffmpeg", "-y", "-loglevel", "error", *args], check=True, capture_output=True, timeout=60
    )


@pytest.fixture
def upright_phone_video(tmp_path) -> Path:
    """1920x1080 frames with a 90° display matrix: how a phone held upright records."""
    landscape = tmp_path / "landscape.mp4"
    _ffmpeg(
        "-f", "lavfi", "-i", "testsrc=duration=3:size=1920x1080:rate=10",
        "-c:v", "libx264", "-pix_fmt", "yuv420p", str(landscape),
    )  # fmt: skip
    rotated = tmp_path / "upright.mp4"
    _ffmpeg("-display_rotation", "90", "-i", str(landscape), "-c", "copy", str(rotated))
    return rotated


class LocalStore:
    """Object storage as a directory, so the processor runs end to end."""

    def __init__(self, root: Path, source: Path) -> None:
        self.root = root
        self.source = source
        self.uploads: dict[str, tuple[Path, str]] = {}

    def object_exists(self, key: str) -> bool:
        return True

    def download_file(self, *, key: str, destination: str) -> None:
        shutil.copyfile(self.source, destination)

    def upload_file(self, *, key: str, source: str, content_type: str = "video/mp4") -> None:
        kept = self.root / key.replace("/", "_")
        shutil.copyfile(source, kept)
        self.uploads[key] = (kept, content_type)


def _size(path: Path) -> tuple[int, int]:
    out = subprocess.run(
        ["ffprobe", "-v", "error", "-select_streams", "v:0", "-show_entries",
         "stream=width,height", "-of", "csv=p=0", str(path)],
        check=True, capture_output=True, text=True,
    ).stdout.strip()  # fmt: skip
    width, height = out.split(",")
    return int(width), int(height)


def test_an_upright_phone_video_is_probed_and_thumbnailed_portrait(upright_phone_video, tmp_path):
    probe = probe_source(upright_phone_video)
    assert (probe.width, probe.height) == (1080, 1920)

    thumbnail = extract_thumbnail(upright_phone_video, tmp_path / "t.jpg", probe)
    width, height = _size(thumbnail)
    assert height == 360 and width < height


def test_the_processor_stores_dimensions_that_match_its_own_mp4(upright_phone_video, tmp_path):
    store = LocalStore(tmp_path, upright_phone_video)
    processor = FfmpegProcessor(
        store,
        output_prefix="outputs",
        max_height=720,
        prober=probe_source,
        source_check=partial(validate_source, max_duration_seconds=300),
        thumbnailer=extract_thumbnail,
    )
    job_id = uuid4()

    result = processor.run(job_id=job_id, source_key="uploads/upright.mp4")

    mp4, _ = store.uploads[result.output_key]
    mp4_width, mp4_height = _size(mp4)
    assert (result.width, result.height) == (1080, 1920)
    assert mp4_height == 720 and mp4_width < mp4_height, "the MP4 is portrait too"
    assert result.thumbnail_key == f"outputs/{job_id}/thumbnail.jpg"
    assert store.uploads[result.thumbnail_key][1] == "image/jpeg"


def test_a_source_over_the_limit_is_refused_without_an_encode(upright_phone_video, tmp_path):
    store = LocalStore(tmp_path, upright_phone_video)
    processor = FfmpegProcessor(
        store,
        output_prefix="outputs",
        prober=probe_source,
        source_check=partial(validate_source, max_duration_seconds=2),
        thumbnailer=extract_thumbnail,
    )

    with pytest.raises(ObjectStoreError) as caught:
        processor.run(job_id=uuid4(), source_key="uploads/upright.mp4")

    assert "the limit is 2 seconds" in caught.value.user_message
    assert store.uploads == {}


def test_an_audio_only_file_is_refused_with_a_message_about_the_file(tmp_path):
    audio = tmp_path / "song.m4a"
    _ffmpeg("-f", "lavfi", "-i", "sine=duration=2", "-c:a", "aac", str(audio))

    with pytest.raises(ObjectStoreError) as caught:
        probe_source(audio)

    assert "no video track" in caught.value.user_message
