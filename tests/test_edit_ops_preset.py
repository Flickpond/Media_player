"""The `preset` edit operation (sprint 6, Track E).

The schema is the API's contract, the rule keeps the frame unchanged, and the
filter module is the worker's half. The browser panel has its own tests in
frontend/preset.test.js; these are the server-side promises it relies on.
"""

from pathlib import Path

import pytest
from pydantic import ValidationError

from app.schemas.edit import EditRequest, PresetParams
from app.services.media_rules import (
    OPERATION_RULES,
    Frame,
    MediaRuleError,
    check_edit_operations,
)
from app.worker.edit_ops import FILTERS, filter_chain
from app.worker.edit_ops.preset import PRESET_FILTERS
from app.worker.operations import build_edit_command

PRESET_NAMES = ("bw", "warm", "cool", "vintage", "vivid")


def request_with(name: str) -> EditRequest:
    return EditRequest.model_validate(
        {"operations": [{"operation": "preset", "params": {"name": name}}]}
    )


@pytest.mark.parametrize("name", PRESET_NAMES)
def test_each_agreed_preset_name_parses(name):
    request = request_with(name)

    assert request.operations[0].params == PresetParams(name=name)


@pytest.mark.parametrize(
    "params",
    [
        {"name": "sepia"},
        {"name": "WARM"},
        {"name": "warm", "strength": 2},
        {},
    ],
)
def test_an_unknown_or_extra_preset_parameter_is_refused(params):
    with pytest.raises(ValidationError):
        EditRequest.model_validate({"operations": [{"operation": "preset", "params": params}]})


def test_a_preset_leaves_the_frame_alone():
    rule = OPERATION_RULES["preset"]

    assert rule.check({"name": "warm"}, Frame(1920, 1080)) == Frame(1920, 1080)
    assert rule.changes_picture is True


def test_the_five_presets_have_exact_filter_chains():
    assert set(PRESET_FILTERS) == set(PRESET_NAMES)
    assert PRESET_FILTERS["bw"] == ("eq=saturation=0",)
    assert PRESET_FILTERS["warm"] == (
        "eq=saturation=1.12:brightness=0.02",
        "colorbalance=rm=0.08:bm=-0.08",
    )
    assert PRESET_FILTERS["cool"] == (
        "eq=saturation=1.05:brightness=0.01",
        "colorbalance=rm=-0.06:bm=0.08",
    )
    assert PRESET_FILTERS["vintage"] == (
        "eq=saturation=0.82:contrast=0.95",
        "curves=preset=vintage",
    )
    assert PRESET_FILTERS["vivid"] == (
        "eq=saturation=1.38:contrast=1.12",
        "colorbalance=rm=0.03:bm=0.03",
    )


def test_every_preset_is_registered_with_the_worker():
    assert FILTERS["preset"]({"name": "vivid"}, Frame(None, None)).video == PRESET_FILTERS[
        "vivid"
    ]


def test_a_preset_runs_after_geometry_and_before_speed_in_the_fixed_order():
    chain = filter_chain(
        [
            ("preset", {"name": "warm"}),
            ("crop", {"x": 0, "y": 0, "w": 640, "h": 360}),
        ],
        Frame(1920, 1080),
    )

    assert chain.before_tone_map == ("crop=640:360:0:0",)
    assert chain.after_tone_map == PRESET_FILTERS["warm"]


def test_the_worker_command_carries_the_preset_filter_chain():
    command = build_edit_command(
        request_with("vintage"),
        ffmpeg_binary="ffmpeg",
        source_path=Path("/source"),
        output_path=Path("/output.mp4"),
        preset="veryfast",
        crf=23,
    )
    video_filter = command[command.index("-vf") + 1]

    assert "eq=saturation=0.82:contrast=0.95" in video_filter
    assert "curves=preset=vintage" in video_filter


def test_a_preset_cannot_be_combined_with_mp3_conversion():
    with pytest.raises(MediaRuleError, match="preset cannot be combined with MP3 conversion"):
        check_edit_operations(
            [("preset", {"name": "bw"}), ("convert", {"format": "mp3"})],
            input_height=1080,
        )
