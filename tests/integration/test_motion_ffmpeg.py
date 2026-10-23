"""Real geometry, timing and pitch checks; no datastores or network required."""

import array
import json
import os
import shutil
import subprocess

import pytest

from app.schemas.edit import EditRequest
from app.services.media_rules import Frame
from app.worker.operations import build_edit_command

pytestmark = pytest.mark.skipif(
    os.getenv("RUN_POSTGRES_TESTS") != "1" or not shutil.which("ffmpeg")
    or not shutil.which("ffprobe"),
    reason="set RUN_POSTGRES_TESTS=1 and install FFmpeg to run real media checks",
)


def run(command):
    return subprocess.run(command, check=True, capture_output=True, timeout=60)


@pytest.fixture(scope="module")
def source(tmp_path_factory):
    path = tmp_path_factory.mktemp("motion") / "source.mp4"
    run(["ffmpeg", "-v", "error", "-f", "lavfi", "-i",
         "color=red:s=320x240:r=30:d=6,drawbox=x=160:y=0:w=160:h=240:color=blue:t=fill",
         "-f", "lavfi", "-i", "sine=frequency=440:sample_rate=48000:duration=6",
         "-c:v", "libx264", "-pix_fmt", "yuv420p", "-c:a", "aac", "-y", str(path)])
    return path


def encode(source, tmp_path, operations, extension="mp4"):
    output = tmp_path / f"result.{extension}"
    parsed = EditRequest.model_validate({"operations": [
        {"operation": name, "params": params} for name, params in operations
    ]})
    command = build_edit_command(parsed, ffmpeg_binary="ffmpeg", source_path=source,
                                 output_path=output, preset="ultrafast", crf=18,
                                 frame=Frame(320, 240))
    run(command)
    info = json.loads(run(["ffprobe", "-v", "error", "-show_streams", "-show_format",
                           "-of", "json", str(output)]).stdout)
    return output, info


@pytest.mark.parametrize("degrees,size", [(90, (240, 320)), (180, (320, 240)), (270, (240, 320))])
def test_real_rotations_have_expected_dimensions(source, tmp_path, degrees, size):
    _, info = encode(source, tmp_path, [("rotate", {"degrees": degrees})])
    video = next(s for s in info["streams"] if s["codec_type"] == "video")
    assert (video["width"], video["height"]) == size


@pytest.mark.parametrize("axis,expected_blue", [("horizontal", True), ("vertical", False)])
def test_real_mirror_changes_the_correct_axis(source, tmp_path, axis, expected_blue):
    output, _ = encode(source, tmp_path, [("flip", {"axis": axis})])
    pixels = run(["ffmpeg", "-v", "error", "-i", str(output), "-frames:v", "1",
                  "-f", "rawvideo", "-pix_fmt", "rgb24", "pipe:1"]).stdout
    offset = (120 * 320 + 40) * 3
    red, _, blue = pixels[offset:offset + 3]
    assert (blue > red) == expected_blue


@pytest.mark.parametrize("factor", [0.5, 0.75, 1, 1.25, 1.5, 1.75, 2])
def test_real_clipped_speed_has_expected_duration_and_440hz_pitch(source, tmp_path, factor):
    output, info = encode(source, tmp_path, [("speed", {"factor": factor}),
                                           ("clip", {"start": 1, "end": 3})])
    assert float(info["format"]["duration"]) == pytest.approx(2 / factor, abs=0.15)
    audio = run(["ffmpeg", "-v", "error", "-i", str(output), "-vn", "-ac", "1", "-ar", "48000",
                 "-f", "s16le", "pipe:1"]).stdout
    samples = array.array("h", audio)[4800:-4800]
    crossings = sum(a <= 0 < b for a, b in zip(samples, samples[1:], strict=False))
    assert crossings * 48000 / len(samples) == pytest.approx(440, abs=5)


def test_real_silent_video_supports_speed_without_an_audio_stream(source, tmp_path):
    silent = tmp_path / "silent.mp4"
    run(["ffmpeg", "-v", "error", "-i", str(source), "-an", "-c:v", "copy", str(silent)])
    _, info = encode(silent, tmp_path, [("speed", {"factor": 2})])
    assert float(info["format"]["duration"]) == pytest.approx(3, abs=0.15)
    assert all(s["codec_type"] != "audio" for s in info["streams"])


def test_real_speed_and_mp3_keeps_only_pitch_preserved_audio(source, tmp_path):
    _, info = encode(source, tmp_path, [("clip", {"start": 1, "end": 3}),
                                      ("speed", {"factor": 0.5}),
                                      ("convert", {"format": "mp3"})], "mp3")
    assert float(info["format"]["duration"]) == pytest.approx(4, abs=0.15)
    assert all(s["codec_type"] == "audio" for s in info["streams"])


def test_real_crop_rotate_scale_and_speed_compose_in_fixed_order(source, tmp_path):
    _, info = encode(source, tmp_path, [
        ("speed", {"factor": 1.5}), ("downscale", {"height": 240}),
        ("rotate", {"degrees": 90}), ("crop", {"x": 0, "y": 0, "w": 300, "h": 200}),
        ("clip", {"start": 1, "end": 3}),
    ])
    video = next(s for s in info["streams"] if s["codec_type"] == "video")
    assert (video["width"], video["height"]) == (160, 240)
    assert float(info["format"]["duration"]) == pytest.approx(2 / 1.5, abs=0.15)
