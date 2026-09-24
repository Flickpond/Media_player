"""C's validation seam rejects unsafe edits without invoking FFmpeg."""

from copy import deepcopy
from unittest.mock import Mock

import pytest

from app.worker.probe import SourceProbe
from app.worker.storage import ObjectStoreError
from app.worker.tasks import readable_error
from app.worker.validation import validate_clip, validate_crop

LANDSCAPE = SourceProbe(width=1920, height=1080, duration_seconds=15.5)
PORTRAIT = SourceProbe(width=1080, height=1920, duration_seconds=600.0)


@pytest.mark.parametrize(
    ("params", "probe"),
    [
        ({"x": 0, "y": 0, "w": 1920, "h": 1080}, LANDSCAPE),
        ({"x": 1918, "y": 1078, "w": 2, "h": 2}, LANDSCAPE),
        ({"x": 0, "y": 140, "w": 1080, "h": 1080}, PORTRAIT),
        ({"x": 0, "y": 0, "w": 1080, "h": 1920}, PORTRAIT),
    ],
)
def test_crop_uses_real_source_pixels_and_accepts_exact_frame_edges(params, probe):
    before = deepcopy(params)
    assert validate_crop(params, probe) is None
    assert params == before


@pytest.mark.parametrize(
    ("changes", "message"),
    [
        ({"x": -1}, "zero or greater"),
        ({"y": -1}, "zero or greater"),
        ({"w": 0}, "greater than zero"),
        ({"h": -2}, "greater than zero"),
        ({"x": 1920}, "fit within"),
        ({"y": 1080}, "fit within"),
        ({"w": 1921}, "fit within"),
        ({"h": 1081}, "fit within"),
        ({"x": 1821, "w": 100}, "fit within"),
        ({"y": 981, "h": 100}, "fit within"),
        ({"x": 10**1000}, "fit within"),
    ],
)
def test_crop_rejects_nonpositive_or_out_of_frame_rectangles(changes, message):
    params = {"x": 0, "y": 0, "w": 100, "h": 100} | changes
    with pytest.raises(ValueError, match=message):
        validate_crop(params, LANDSCAPE)


@pytest.mark.parametrize("key", ["x", "y", "w", "h"])
@pytest.mark.parametrize("value", [True, None, 2.0, "100", "iw/2", float("nan"), []])
def test_crop_rejects_coercion_and_ffmpeg_expressions(key, value):
    params = {"x": 0, "y": 0, "w": 100, "h": 100} | {key: value}
    with pytest.raises(ValueError, match="whole numbers"):
        validate_crop(params, LANDSCAPE)


@pytest.mark.parametrize(
    "params", [None, [], {}, {"x": 0, "y": 0, "w": 1}, {"x": 0, "y": 0, "w": 1, "h": 1, "extra": 0}]
)
def test_crop_requires_exactly_the_contracts_four_fields(params):
    with pytest.raises(ValueError, match="requires x, y, w and h"):
        validate_crop(params, LANDSCAPE)


@pytest.mark.parametrize("width,height", [(0, 1080), (1920, -1), (True, 1080)])
def test_crop_cannot_proceed_with_an_unusable_probe(width, height):
    with pytest.raises(ValueError, match="dimensions could not be determined"):
        validate_crop({"x": 0, "y": 0, "w": 2, "h": 2}, SourceProbe(width, height, 10))


@pytest.mark.parametrize(
    "params", [{"start": 0, "end": 15.5}, {"start": 5.0, "end": 12.5}, {"start": 15.0, "end": 15.5}]
)
def test_clip_preserves_seconds_and_accepts_the_exact_duration(params):
    before = deepcopy(params)
    assert validate_clip(params, LANDSCAPE) is None
    assert params == before


@pytest.mark.parametrize(
    ("params", "message"),
    [
        ({"start": -0.1, "end": 2}, "zero or greater"),
        ({"start": 2, "end": 2}, "greater than start"),
        ({"start": 3, "end": 2}, "greater than start"),
        ({"start": 0, "end": 15.5001}, "must not exceed"),
    ],
)
def test_clip_rejects_invalid_ranges(params, message):
    with pytest.raises(ValueError, match=message):
        validate_clip(params, LANDSCAPE)


@pytest.mark.parametrize("key", ["start", "end"])
@pytest.mark.parametrize(
    "value", [True, None, "5.0", [], float("nan"), float("inf"), -float("inf"), 10**1000]
)
def test_clip_cannot_pass_nonfinite_or_non_numeric_values_to_ffmpeg(key, value):
    with pytest.raises(ValueError, match="finite numbers"):
        validate_clip({"start": 0, "end": 10} | {key: value}, LANDSCAPE)


@pytest.mark.parametrize(
    "params", [None, [], {}, {"start": 0}, {"start": 0, "end": 10, "extra": 1}]
)
def test_clip_requires_exactly_start_and_end(params):
    with pytest.raises(ValueError, match="requires start and end"):
        validate_clip(params, LANDSCAPE)


@pytest.mark.parametrize("duration", [0, -1, float("nan"), float("inf"), True, "10"])
def test_clip_refuses_unknown_or_nonfinite_source_duration(duration):
    with pytest.raises(ValueError, match="duration could not be determined"):
        validate_clip({"start": 0, "end": 1}, SourceProbe(1920, 1080, duration))


def test_bs_documented_adapter_keeps_the_message_readable_and_never_runs_bad_edits():
    """Prove the handoff, not an assertion that B's processor already exists."""
    ffmpeg = Mock()
    with pytest.raises(ObjectStoreError) as failure:
        try:
            validate_crop({"x": 0, "y": 0, "w": 1921, "h": 1080}, LANDSCAPE)
        except ValueError as exc:
            raise ObjectStoreError("crop validation failed", user_message=str(exc)) from exc
        ffmpeg()
    ffmpeg.assert_not_called()
    assert readable_error(failure.value) == "crop must fit within the source video's frame"
