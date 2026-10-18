"""The edit-operation seam (sprint 6 plan, 2.1).

The stand-in `rotate` and `speed` here are registered for one test and removed
after it. They are also the shortest worked example of adding an operation:
a rule that says what the frame becomes, and filters for -vf or -af.
"""

from pathlib import Path
from types import SimpleNamespace

import pytest

from app.schemas.edit import ConvertParams, EditRequest
from app.services.media_rules import (
    OPERATION_ORDER,
    OPERATION_RULES,
    Frame,
    MediaRuleError,
    OperationRule,
    check_edit_operations,
    ordered,
    register_rule,
)
from app.worker.edit_ops import FILTERS, FilterStep, filter_chain, register_filter
from app.worker.operations import build_edit_command
from app.worker.storage import ObjectStoreError


@pytest.fixture
def stand_ins():
    """A rotate that turns the frame and a speed that changes only the sound."""

    def rotate_rule(params, frame):
        if params["degrees"] in (90, 270):
            return Frame(width=frame.height, height=frame.width)
        return frame

    register_rule("rotate", OperationRule(label="rotate", check=rotate_rule))
    register_filter("rotate", lambda params, _f: FilterStep(video=("transpose=1",)))
    register_rule(
        "speed",
        OperationRule(label="speed", check=lambda _p, frame: frame, changes_picture=False),
    )
    register_filter(
        "speed",
        lambda params, _f: FilterStep(
            video=(f"setpts=PTS/{params['factor']}",), audio=(f"atempo={params['factor']}",)
        ),
    )
    yield
    for name in ("rotate", "speed"):
        OPERATION_RULES.pop(name, None)
        FILTERS.pop(name, None)


def test_operations_run_in_the_fixed_order_whatever_order_they_are_sent_in():
    operations = [
        ("downscale", {"height": 360}),
        ("crop", {"x": 0, "y": 0, "w": 640, "h": 480}),
    ]

    chain = filter_chain(operations, Frame(1920, 1080))

    assert chain.video == ("crop=640:480:0:0", "scale=-2:360")
    assert chain.audio == ()
    assert ordered(["convert", "crop", "clip"]) == ["clip", "crop", "convert"]


def test_the_order_has_a_place_for_every_planned_operation():
    for name in ("rotate", "flip", "adjust", "preset", "speed"):
        assert name in OPERATION_ORDER
    # Crop before rotation keeps its box in the original's pixels.
    assert OPERATION_ORDER.index("crop") < OPERATION_ORDER.index("rotate")
    assert OPERATION_ORDER.index("upscale") < OPERATION_ORDER.index("adjust")


def test_a_scale_after_a_rotation_is_judged_against_the_rotated_frame(stand_ins):
    """1920x1080 turned on its side is 1920 tall: 1080 is now a downscale."""
    rotated = [("rotate", {"degrees": 90}), ("downscale", {"height": 1080})]
    upright = [("downscale", {"height": 1080})]

    assert check_edit_operations(rotated, input_height=1080, input_width=1920) is None
    with pytest.raises(MediaRuleError, match="below the video's 1080p"):
        check_edit_operations(upright, input_height=1080, input_width=1920)


def test_a_rotation_is_filtered_after_the_crop_and_before_the_scale(stand_ins):
    operations = [
        ("downscale", {"height": 720}),
        ("rotate", {"degrees": 90}),
        ("crop", {"x": 0, "y": 0, "w": 1080, "h": 1920}),
    ]

    chain = filter_chain(operations, Frame(1920, 1080))

    assert chain.video == ("crop=1080:1920:0:0", "transpose=1", "scale=-2:720")


def request_with(*operations):
    """A request holding operations the schema does not know yet: each
    owner adds theirs to `EditOperation` with their feature."""
    items = [
        SimpleNamespace(
            operation=name,
            params=ConvertParams(**params)
            if name == "convert"
            else SimpleNamespace(model_dump=lambda mode=None, params=params: params),
        )
        for name, params in operations
    ]
    return EditRequest.model_construct(operations=items)


def build(request, output="/o.mp4"):
    return build_edit_command(
        request,
        ffmpeg_binary="ffmpeg",
        source_path=Path("/s"),
        output_path=Path(output),
        preset="veryfast",
        crf=23,
    )


def test_an_operations_audio_filters_go_into_af(stand_ins):
    command = build(request_with(("speed", {"factor": 1.5})))

    assert "setpts=PTS/1.5" in command[command.index("-vf") + 1]
    assert command[command.index("-af") + 1] == "atempo=1.5"


def test_mp3_keeps_a_sound_change_and_drops_its_picture_filters(stand_ins):
    command = build(
        request_with(("speed", {"factor": 2.0}), ("convert", {"format": "mp3"})), "/o.mp3"
    )

    assert "-vn" in command and "-vf" not in command
    assert command[command.index("-af") + 1] == "atempo=2.0"


def test_mp3_still_refuses_a_picture_change_that_reached_the_worker(stand_ins):
    with pytest.raises(ObjectStoreError, match="audio-only"):
        build(request_with(("rotate", {"degrees": 90}), ("convert", {"format": "mp3"})), "/o.mp3")


def test_an_audio_filter_stops_an_mkv_from_being_a_plain_remux(stand_ins):
    remux = build(request_with(("convert", {"format": "mkv"})), "/o.mkv")
    retimed = build(
        request_with(("speed", {"factor": 1.5}), ("convert", {"format": "mkv"})), "/o.mkv"
    )

    assert remux[remux.index("-c") + 1] == "copy"
    assert "copy" not in retimed and "-af" in retimed


def test_colour_runs_after_the_tone_map_and_geometry_before_it(stand_ins):
    """An HDR upload: crop on the decoded source, the tone-map, then the
    stand-in speed (placed after `adjust`) on the 8-bit picture."""
    command = build(
        request_with(("speed", {"factor": 1.5}), ("crop", {"x": 0, "y": 0, "w": 640, "h": 360})),
    )
    plain = command[command.index("-vf") + 1].split(",")

    assert plain.index("crop=640:360:0:0") < plain.index("format=yuv420p")
    assert plain.index("format=yuv420p") < plain.index("setpts=PTS/1.5")

    hdr = build_edit_command(
        request_with(("speed", {"factor": 1.5}), ("crop", {"x": 0, "y": 0, "w": 640, "h": 360})),
        ffmpeg_binary="ffmpeg",
        source_path=Path("/s"),
        output_path=Path("/o.mp4"),
        preset="veryfast",
        crf=23,
        hdr=True,
    )
    chain = hdr[hdr.index("-vf") + 1]
    assert chain.index("crop=") < chain.index("tonemap=") < chain.index("setpts=")


def test_registration_refuses_an_unplaced_a_duplicate_or_an_unchecked_operation():
    with pytest.raises(ValueError, match="no place"):
        register_rule("sharpen", OperationRule(label="sharpen", check=lambda p, f: f))
    with pytest.raises(ValueError, match="twice"):
        register_rule("crop", OperationRule(label="crop", check=lambda p, f: f))
    with pytest.raises(ValueError, match="no rule"):
        register_filter("flip", lambda p, f: FilterStep())


def test_mp3_names_every_picture_operation_asked_for():
    request = [
        ("crop", {"x": 0, "y": 0, "w": 10, "h": 10}),
        ("downscale", {"height": 240}),
        ("convert", {"format": "mp3"}),
    ]

    with pytest.raises(MediaRuleError, match="^crop and scale cannot be combined with MP3"):
        check_edit_operations(request, input_height=720)


def test_the_frame_after_a_scale_keeps_the_shape_and_an_even_width():
    rule = OPERATION_RULES["downscale"]

    assert rule.check({"height": 720}, Frame(1920, 1080)) == Frame(1280, 720)
    assert rule.check({"height": 360}, Frame(1080, 1920)) == Frame(202, 360)
    assert rule.check({"height": 360}, Frame(None, 1080)) == Frame(None, 360)


def test_a_request_may_hold_one_of_every_operation():
    assert EditRequest.model_fields["operations"].metadata[1].max_length == len(OPERATION_ORDER)
