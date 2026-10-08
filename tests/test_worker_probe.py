import json
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

from app.worker.probe import NO_VIDEO_STREAM, probe_source
from app.worker.storage import UNPROCESSABLE_VIDEO, ObjectStoreError

SOURCE = Path("/tmp/source.mp4")


def ffprobe_output(*, width=1920, height=1080, duration="12.5") -> str:
    return json.dumps(
        {
            "streams": [{"width": width, "height": height}],
            "format": {"duration": duration},
        }
    )


def runner_returning(stdout: str, *, returncode: int = 0, stderr: str = ""):
    def runner(command, **_kwargs):
        runner.command = command
        return SimpleNamespace(returncode=returncode, stdout=stdout, stderr=stderr)

    runner.command = []
    return runner


# Captured verbatim from ffprobe 7.1.5 (Debian 13, the worker image) against
# a real 640x360 3-second H.264 file. Hand-written payloads elsewhere in this
# file test the failure paths; this one pins the happy path to output the
# tool actually produces -- note `duration` arrives as a string, and the
# empty `programs`/`stream_groups` keys ride along whether asked for or not.
REAL_FFPROBE_OUTPUT = """{
    "programs": [

    ],
    "stream_groups": [

    ],
    "streams": [
        {
            "width": 640,
            "height": 360
        }
    ],
    "format": {
        "duration": "3.000000"
    }
}"""


def test_probe_parses_real_ffprobe_output():
    probe = probe_source(SOURCE, runner=runner_returning(REAL_FFPROBE_OUTPUT))

    assert (probe.width, probe.height) == (640, 360)
    assert probe.duration_seconds == 3.0


def test_probe_reads_width_height_and_duration():
    probe = probe_source(SOURCE, runner=runner_returning(ffprobe_output()))

    assert probe.width == 1920
    assert probe.height == 1080
    assert probe.duration_seconds == 12.5


def test_probe_asks_for_one_combined_entry_spec():
    """The colon-separated spec is the form ffprobe documents.

    Splitting it across two `-show_entries` flags happens to work on the
    image's current ffprobe too, so this is not pinning a bug -- it is
    pinning the documented form, because if that ever stopped accumulating
    the half we lost would arrive as an absent key rather than an error,
    and read as "this file has no duration" instead of "the command was
    wrong."
    """
    runner = runner_returning(ffprobe_output())

    probe_source(SOURCE, runner=runner)

    assert runner.command.count("-show_entries") == 1
    spec = runner.command[runner.command.index("-show_entries") + 1]
    assert spec == (
        "stream=width,height:stream_side_data=rotation:stream_tags=rotate:format=duration"
    )


def test_probe_selects_only_the_first_video_stream():
    """`-select_streams v:0`, so a file whose first stream is audio still
    reports the video's dimensions rather than nothing.
    """
    runner = runner_returning(ffprobe_output())

    probe_source(SOURCE, runner=runner)

    assert "-select_streams" in runner.command
    assert runner.command[runner.command.index("-select_streams") + 1] == "v:0"


def test_probe_uses_the_configured_binary():
    runner = runner_returning(ffprobe_output())

    probe_source(SOURCE, ffprobe_binary="/opt/custom/ffprobe", runner=runner)

    assert runner.command[0] == "/opt/custom/ffprobe"


def test_a_nonzero_exit_is_reported_as_unprocessable():
    runner = runner_returning("", returncode=1, stderr="moov atom not found")

    with pytest.raises(ObjectStoreError, match="moov atom not found") as caught:
        probe_source(SOURCE, runner=runner)

    assert caught.value.user_message == UNPROCESSABLE_VIDEO


def test_a_timeout_is_reported_rather_than_raised_raw():
    def runner(_command, **_kwargs):
        raise subprocess.TimeoutExpired(cmd="ffprobe", timeout=30)

    with pytest.raises(ObjectStoreError, match="timed out") as caught:
        probe_source(SOURCE, timeout_seconds=30, runner=runner)

    assert caught.value.user_message == UNPROCESSABLE_VIDEO


def test_a_file_with_no_video_stream_is_refused():
    payload = json.dumps({"streams": [], "format": {"duration": "10.0"}})

    with pytest.raises(ObjectStoreError, match="no video stream"):
        probe_source(SOURCE, runner=runner_returning(payload))


def test_output_that_is_not_json_is_refused():
    with pytest.raises(ObjectStoreError, match="not JSON"):
        probe_source(SOURCE, runner=runner_returning("<html>proxy error</html>"))


def test_a_missing_duration_is_refused_rather_than_defaulted():
    """A clip range is validated against this number. Guessing a default
    would validate against a duration the file does not have.
    """
    payload = json.dumps({"streams": [{"width": 640, "height": 360}], "format": {}})

    with pytest.raises(ObjectStoreError, match="missing width, height or duration"):
        probe_source(SOURCE, runner=runner_returning(payload))


def test_a_missing_dimension_is_refused():
    payload = json.dumps({"streams": [{"width": 640}], "format": {"duration": "3.0"}})

    with pytest.raises(ObjectStoreError, match="missing width, height or duration"):
        probe_source(SOURCE, runner=runner_returning(payload))


@pytest.mark.parametrize(
    ("width", "height", "duration"),
    [(0, 360, "3.0"), (640, 0, "3.0"), (640, 360, "0")],
)
def test_a_non_positive_dimension_or_duration_is_refused(width, height, duration):
    """A zero height would make the HLS ladder's variant choice divide by
    nothing, and a zero duration makes every clip range invalid. Refuse at
    the edge rather than propagating it.
    """
    payload = ffprobe_output(width=width, height=height, duration=duration)

    with pytest.raises(ObjectStoreError, match="non-positive"):
        probe_source(SOURCE, runner=runner_returning(payload))


def test_the_user_message_never_carries_the_path_or_ffprobe_output():
    """P9's rule, applied here: the diagnostic half names the file and the
    stderr, the user-facing half names neither.
    """
    runner = runner_returning("", returncode=1, stderr="/tmp/source.mp4: Invalid data found")

    with pytest.raises(ObjectStoreError) as caught:
        probe_source(SOURCE, runner=runner)

    assert "source.mp4" in str(caught.value)
    assert "source.mp4" not in caught.value.user_message
    assert "Invalid data" not in caught.value.user_message


# --- sprint 4 (Track C): a phone video, the right way up --------------------

# Captured from ffprobe 6.1 against a 1920x1080 H.264 file carrying a 90°
# display matrix -- what an upright phone recording looks like to ffprobe.
REAL_ROTATED_FFPROBE_OUTPUT = """{
    "programs": [],
    "streams": [
        {"width": 1920, "height": 1080, "tags": {}, "side_data_list": [{"rotation": 90}]}
    ],
    "format": {"duration": "3.000000"}
}"""


def test_an_upright_phone_video_reports_the_size_it_is_shown_at():
    """FFmpeg rotates before any filter runs, so the encode, the ladder, the
    thumbnail and the editor all see 1080x1920. So must everything sized
    from this probe.
    """
    probe = probe_source(SOURCE, runner=runner_returning(REAL_ROTATED_FFPROBE_OUTPUT))

    assert (probe.width, probe.height) == (1080, 1920)


@pytest.mark.parametrize(
    ("stream_extra", "expected"),
    [
        ({"side_data_list": [{"rotation": -90}]}, (1080, 1920)),
        ({"side_data_list": [{"rotation": 270}]}, (1080, 1920)),
        ({"side_data_list": [{"rotation": -180}]}, (1920, 1080)),
        ({"side_data_list": [{"displaymatrix": "..."}]}, (1920, 1080)),
        ({"side_data_list": [{"rotation": "sideways"}]}, (1920, 1080)),
        ({"tags": {"rotate": "90"}}, (1080, 1920)),
        ({"tags": {"rotate": "not a number"}}, (1920, 1080)),
    ],
)
def test_every_way_a_rotation_is_recorded_is_honoured(stream_extra, expected):
    payload = json.dumps(
        {
            "streams": [{"width": 1920, "height": 1080, **stream_extra}],
            "format": {"duration": "5.0"},
        }
    )

    probe = probe_source(SOURCE, runner=runner_returning(payload))

    assert (probe.width, probe.height) == expected


def test_a_file_with_no_video_stream_tells_the_uploader_what_to_do_about_it():
    payload = json.dumps({"streams": [], "format": {"duration": "10.0"}})

    with pytest.raises(ObjectStoreError) as caught:
        probe_source(SOURCE, runner=runner_returning(payload))

    assert caught.value.user_message == NO_VIDEO_STREAM
