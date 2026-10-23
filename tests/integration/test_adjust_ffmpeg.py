"""Colour adjustments against real FFmpeg: the worker's own edit command, run
on a short clip, measured with `signalstats`.

Gated like every integration test, and on ffmpeg/ffprobe being installed.
"""

import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest

from app.schemas.edit import EditRequest
from app.worker.operations import build_edit_command

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


def _run(command: list[str]) -> str:
    done = subprocess.run(command, check=True, capture_output=True, text=True, timeout=120)
    return done.stdout


@pytest.fixture
def clip(tmp_path) -> Path:
    """Two seconds of colour bars and gradients, with sound, as an upload's MP4 is."""
    path = tmp_path / "bars.mp4"
    _run(
        [
            "ffmpeg", "-y", "-loglevel", "error",
            "-f", "lavfi", "-i", "testsrc2=duration=2:size=640x360:rate=25",
            "-f", "lavfi", "-i", "sine=frequency=440:duration=2",
            "-c:v", "libx264", "-pix_fmt", "yuv420p", "-c:a", "aac", "-shortest", str(path),
        ]
    )  # fmt: skip
    return path


def edit(source: Path, output: Path, params: dict) -> Path:
    request = EditRequest.model_validate(
        {"operations": [{"operation": "adjust", "params": params}]}
    )
    command = build_edit_command(
        request,
        ffmpeg_binary="ffmpeg",
        source_path=source,
        output_path=output,
        preset="ultrafast",
        crf=18,
    )
    _run(command)
    return output


def stats(path: Path) -> dict[str, float]:
    """Mean luma, chroma and saturation over the whole video, 8-bit codes."""
    out = _run(
        [
            "ffprobe", "-v", "error", "-f", "lavfi", f"movie={path},signalstats",
            "-show_entries",
            "frame_tags=lavfi.signalstats.YAVG,lavfi.signalstats.SATAVG",
            "-of", "json",
        ]
    )  # fmt: skip
    frames = [frame["tags"] for frame in json.loads(out)["frames"]]
    return {
        key: sum(float(tags[f"lavfi.signalstats.{key}"]) for tags in frames) / len(frames)
        for key in ("YAVG", "SATAVG")
    }


def probe(path: Path) -> dict:
    out = _run(
        [
            "ffprobe", "-v", "error", "-show_entries",
            "stream=codec_type,width,height,pix_fmt:format=duration", "-of", "json", str(path),
        ]
    )  # fmt: skip
    return json.loads(out)


def test_brightness_lifts_the_picture_and_keeps_its_size_length_and_sound(clip, tmp_path):
    before = stats(clip)
    result = edit(clip, tmp_path / "bright.mp4", {"brightness": 0.2})
    after = stats(result)

    # +0.2 is a fifth of eq's 0..1 scale: about 51 codes, less what clips at white.
    assert 30 < after["YAVG"] - before["YAVG"] <= 52
    info = probe(result)
    video = next(s for s in info["streams"] if s["codec_type"] == "video")
    assert (video["width"], video["height"], video["pix_fmt"]) == (640, 360, "yuv420p")
    assert any(s["codec_type"] == "audio" for s in info["streams"])
    assert abs(float(info["format"]["duration"]) - 2.0) < 0.1


def test_saturation_zero_is_black_and_white(clip, tmp_path):
    result = edit(clip, tmp_path / "grey.mp4", {"saturation": 0})

    assert stats(clip)["SATAVG"] > 20
    assert stats(result)["SATAVG"] < 1.5


def test_gamma_above_one_brightens_the_mid_tones(clip, tmp_path):
    before = stats(clip)
    after = stats(edit(clip, tmp_path / "gamma.mp4", {"gamma": 1.6}))

    assert after["YAVG"] > before["YAVG"] + 10
