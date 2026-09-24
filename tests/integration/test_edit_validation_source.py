"""Check C's geometry against ffprobe on a real, non-landscape video."""

import os
import shutil
import subprocess

import pytest

from app.worker.probe import probe_source
from app.worker.validation import validate_clip, validate_crop

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


def test_a_real_portrait_source_is_validated_in_source_pixels_and_seconds(tmp_path):
    source = tmp_path / "portrait.mp4"
    subprocess.run(
        [
            "ffmpeg",
            "-y",
            "-loglevel",
            "error",
            "-f",
            "lavfi",
            "-i",
            "color=c=blue:s=360x640:r=10",
            "-t",
            "2",
            "-c:v",
            "libx264",
            "-pix_fmt",
            "yuv420p",
            str(source),
        ],
        check=True,
        capture_output=True,
        timeout=30,
    )
    probe = probe_source(source)
    assert (probe.width, probe.height) == (360, 640)
    assert probe.duration_seconds == pytest.approx(2.0)

    assert validate_crop({"x": 0, "y": 100, "w": 360, "h": 540}, probe) is None
    assert validate_clip({"start": 0.5, "end": probe.duration_seconds}, probe) is None
    with pytest.raises(ValueError, match="fit within"):
        validate_crop({"x": 0, "y": 0, "w": 640, "h": 360}, probe)
    with pytest.raises(ValueError, match="must not exceed"):
        validate_clip({"start": 0, "end": probe.duration_seconds + 0.001}, probe)
