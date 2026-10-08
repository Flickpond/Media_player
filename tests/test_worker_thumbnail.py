import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

from app.worker.probe import SourceProbe
from app.worker.thumbnail import (
    ThumbnailError,
    build_thumbnail_command,
    extract_thumbnail,
    thumbnail_offset,
)

PROBE = SourceProbe(width=3840, height=2160, duration_seconds=41.2)


@pytest.mark.parametrize(
    ("duration", "expected"), [(41.2, 1.0), (2.0, 1.0), (1.0, 0.5), (0.4, 0.2), (0.0, 0.0)]
)
def test_the_frame_is_a_second_in_or_the_middle_of_a_shorter_clip(duration, expected):
    assert thumbnail_offset(duration) == pytest.approx(expected)


def test_the_seek_happens_before_the_input_so_a_long_file_is_not_decoded_to_it():
    command = build_thumbnail_command(
        source_path=Path("/tmp/source"),
        output_path=Path("/tmp/out.jpg"),
        offset_seconds=1.0,
        max_width=640,
        max_height=360,
    )

    assert command.index("-ss") < command.index("-i")
    assert command[command.index("-frames:v") + 1] == "1"
    assert command[-1] == "/tmp/out.jpg"


def test_the_box_bounds_the_frame_without_enlarging_or_distorting_it():
    command = build_thumbnail_command(
        source_path=Path("s"), output_path=Path("o.jpg"), offset_seconds=0, max_width=640,
        max_height=360,
    )
    scale = command[command.index("-vf") + 1]

    assert "min(640,iw)" in scale and "min(360,ih)" in scale
    assert "force_original_aspect_ratio=decrease" in scale


def _runner(*, returncode=0, write=True, raises=None):
    def runner(command, **_kwargs):
        runner.command = command
        if raises is not None:
            raise raises
        if write:
            Path(command[-1]).write_bytes(b"\xff\xd8jpeg")
        return SimpleNamespace(returncode=returncode, stderr="decoder error")

    return runner


def test_a_frame_is_written_and_its_path_returned(tmp_path):
    output = tmp_path / "thumb.jpg"

    assert extract_thumbnail(tmp_path / "s", output, PROBE, runner=_runner()) == output
    assert output.read_bytes().startswith(b"\xff\xd8")


def test_the_configured_binary_and_box_are_used(tmp_path):
    runner = _runner()

    extract_thumbnail(
        tmp_path / "s",
        tmp_path / "t.jpg",
        PROBE,
        ffmpeg_binary="/opt/ffmpeg",
        max_width=320,
        max_height=180,
        runner=runner,
    )

    assert runner.command[0] == "/opt/ffmpeg"
    assert "min(320,iw)" in runner.command[runner.command.index("-vf") + 1]


def test_a_nonzero_exit_is_a_thumbnail_error(tmp_path):
    with pytest.raises(ThumbnailError, match="decoder error"):
        extract_thumbnail(tmp_path / "s", tmp_path / "t.jpg", PROBE, runner=_runner(returncode=1))


def test_exit_zero_with_no_file_is_still_a_failure(tmp_path):
    """FFmpeg does this when the seek lands past the last decodable frame."""
    with pytest.raises(ThumbnailError, match="no thumbnail"):
        extract_thumbnail(tmp_path / "s", tmp_path / "t.jpg", PROBE, runner=_runner(write=False))


def test_a_timeout_is_a_thumbnail_error_not_a_raw_exception(tmp_path):
    runner = _runner(raises=subprocess.TimeoutExpired("ffmpeg", 30))

    with pytest.raises(ThumbnailError, match="timed out"):
        extract_thumbnail(tmp_path / "s", tmp_path / "t.jpg", PROBE, runner=runner)
