from pathlib import Path

import pytest
from pydantic import ValidationError

from app.schemas.edit import EditRequest
from app.services.media_rules import Frame, MediaRuleError, check_edit_operations
from app.worker.edit_ops import filter_chain
from app.worker.operations import build_edit_command


def request(name, params):
    return EditRequest.model_validate({"operations": [{"operation": name, "params": params}]})


@pytest.mark.parametrize("degrees,filters", [
    (90, ("transpose=1",)), (180, ("hflip", "vflip")), (270, ("transpose=2",)),
])
def test_clockwise_turn_filters(degrees, filters):
    parsed = request("rotate", {"degrees": degrees})
    assert parsed.stored_operations()[0]["params"] == {"degrees": degrees}
    assert filter_chain([("rotate", {"degrees": degrees})], Frame(1920, 1080)).video == filters


@pytest.mark.parametrize("degrees", [0, 360, -90, 45, True, "90", 90.0, None])
def test_rotate_refuses_non_integer_or_unsupported_angles(degrees):
    with pytest.raises(ValidationError):
        request("rotate", {"degrees": degrees})


@pytest.mark.parametrize("axis,expected", [("horizontal", "hflip"), ("vertical", "vflip")])
def test_flip_filters(axis, expected):
    request("flip", {"axis": axis})
    assert filter_chain([("flip", {"axis": axis})], Frame(1920, 1080)).video == (expected,)


@pytest.mark.parametrize("name,params", [
    ("flip", {"axis": "both"}), ("flip", {}),
    ("rotate", {"degrees": 90, "extra": 1}), ("flip", {"axis": True}),
])
def test_parameters_are_strict(name, params):
    with pytest.raises(ValidationError):
        request(name, params)


def test_crop_then_rotate_then_scale_regardless_of_request_order():
    ops = [("downscale", {"height": 720}), ("flip", {"axis": "horizontal"}),
           ("rotate", {"degrees": 90}), ("crop", {"x": 0, "y": 0, "w": 1200, "h": 600})]
    check_edit_operations(ops, input_width=1920, input_height=1080)
    assert filter_chain(ops, Frame(1920, 1080)).video == (
        "crop=1200:600:0:0", "transpose=1", "hflip", "scale=-2:720",
    )
    # Without the quarter turn, 720p would be an upscale from the 600p crop.
    with pytest.raises(MediaRuleError, match="below"):
        check_edit_operations([ops[0], ops[-1]], input_width=1920, input_height=1080)


@pytest.mark.parametrize("degrees", [90, 270])
def test_quarter_turn_checks_scale_against_original_width(degrees):
    with pytest.raises(MediaRuleError, match="below"):
        check_edit_operations([("rotate", {"degrees": degrees}), ("downscale", {"height": 720})],
                              input_width=600, input_height=1200)


def test_half_turn_preserves_frame_and_unknown_dimensions_stay_unknown():
    check_edit_operations([("rotate", {"degrees": 180}), ("downscale", {"height": 720})],
                          input_width=600, input_height=1200)
    check_edit_operations([("rotate", {"degrees": 90}), ("downscale", {"height": 720})],
                          input_height=1200)


@pytest.mark.parametrize("name,params", [("rotate", {"degrees": 90}),
                                         ("flip", {"axis": "vertical"})])
def test_picture_operations_cannot_be_silently_discarded_for_mp3(name, params):
    with pytest.raises(MediaRuleError, match="MP3"):
        check_edit_operations([(name, params), ("convert", {"format": "mp3"})],
                              input_height=1080)


def test_rotating_mkv_reencodes_instead_of_copying():
    parsed = EditRequest.model_validate({"operations": [
        {"operation": "rotate", "params": {"degrees": 90}},
        {"operation": "convert", "params": {"format": "mkv"}},
    ]})
    command = build_edit_command(parsed, ffmpeg_binary="ffmpeg", source_path=Path("in"),
                                 output_path=Path("out.mkv"), preset="veryfast", crf=23)
    assert "transpose=1" in command[command.index("-vf") + 1]
    assert "copy" not in command
