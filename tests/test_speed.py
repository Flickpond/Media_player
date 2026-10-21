from pathlib import Path

import pytest
from pydantic import ValidationError

from app.schemas.edit import EditRequest
from app.services.media_rules import Frame, check_edit_operations
from app.worker.edit_ops import filter_chain
from app.worker.operations import build_edit_command


@pytest.mark.parametrize("factor", [0.5, 0.75, 1, 1.25, 1.5, 1.75, 2])
def test_every_supported_speed_retimes_picture_and_preserves_audio_pitch(factor):
    parsed = EditRequest.model_validate({"operations": [
        {"operation": "speed", "params": {"factor": factor}},
    ]})
    stored = parsed.stored_operations()[0]["params"]
    chain = filter_chain([("speed", stored)], Frame(1920, 1080))
    assert chain.video == (f"setpts=(PTS-STARTPTS)/{float(factor)}",)
    assert chain.audio == ("asetpts=PTS-STARTPTS", f"atempo={float(factor)}")


@pytest.mark.parametrize("factor", [0, 0.25, 2.25, 1.1, "1.5", True, None,
                                    float("inf"), float("nan")])
def test_speed_refuses_coercion_nonfinite_values_and_non_quarter_steps(factor):
    with pytest.raises(ValidationError):
        EditRequest.model_validate({"operations": [
            {"operation": "speed", "params": {"factor": factor}},
        ]})


@pytest.mark.parametrize("format", ["mp4", "mkv", "mp3"])
def test_clip_is_cut_on_original_timeline_before_speed_and_conversion(format):
    operations = [
        {"operation": "speed", "params": {"factor": 0.5}},
        {"operation": "clip", "params": {"start": 2, "end": 4}},
    ]
    if format != "mp4":
        operations.append({"operation": "convert", "params": {"format": format}})
    parsed = EditRequest.model_validate({"operations": operations})
    check_edit_operations([(op.operation, op.params.model_dump()) for op in parsed.operations],
                          input_width=1920, input_height=1080)
    command = build_edit_command(parsed, ffmpeg_binary="ffmpeg", source_path=Path("in"),
                                 output_path=Path(f"out.{format}"), preset="veryfast", crf=23)
    assert command.index("-ss") < command.index("-i")
    assert command[command.index("-to") + 1] == "4"
    assert command[command.index("-af") + 1] == "asetpts=PTS-STARTPTS,atempo=0.5"
    assert "copy" not in command
    if format == "mp3":
        assert "-vn" in command and "-vf" not in command
    else:
        assert "setpts=(PTS-STARTPTS)/0.5" in command[command.index("-vf") + 1]
