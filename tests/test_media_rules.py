import math

import pytest

from app.services.media_rules import (
    MAX_SCALE_HEIGHT,
    SCALE_HEIGHTS,
    MediaRuleError,
    check_edit_operations,
    check_scale,
    check_source_duration,
    describe_limit,
    editable_height,
    format_duration,
    scale_options,
)
from app.worker.probe import SourceProbe
from app.worker.validation import validate_edit_rules, validate_source

# --- the duration limit -----------------------------------------------------


@pytest.mark.parametrize(
    ("seconds", "expected"),
    [(0, "0:00"), (59.2, "1:00"), (300, "5:00"), (754.2, "12:35"), (3725, "1:02:05")],
)
def test_a_duration_is_written_the_way_a_player_shows_it(seconds, expected):
    assert format_duration(seconds) == expected


def test_a_duration_just_over_the_limit_is_never_shown_as_the_limit_itself():
    """300.4 s must not read "5:00 long; the limit is 5 minutes"."""
    assert format_duration(300.4) == "5:01"


@pytest.mark.parametrize(
    ("seconds", "expected"),
    [(60, "1 minute"), (300, "5 minutes"), (90, "90 seconds")],
)
def test_the_limit_is_described_in_words(seconds, expected):
    assert describe_limit(seconds) == expected


def test_a_source_within_the_limit_passes():
    assert check_source_duration(299.9, max_duration_seconds=300) is None
    assert check_source_duration(300.0, max_duration_seconds=300) is None


def test_a_source_over_the_limit_is_refused_with_its_length_and_the_limit():
    with pytest.raises(MediaRuleError) as caught:
        check_source_duration(754.2, max_duration_seconds=300)

    message = str(caught.value)
    assert "12:35" in message
    assert "5 minutes" in message


def test_a_limit_of_zero_turns_the_check_off():
    assert check_source_duration(10_000, max_duration_seconds=0) is None


@pytest.mark.parametrize("duration", [0.0, -1.0, math.nan, math.inf])
def test_a_length_that_cannot_be_trusted_is_refused_rather_than_waved_through(duration):
    with pytest.raises(MediaRuleError, match="length could not be determined"):
        check_source_duration(duration, max_duration_seconds=300)


def test_the_worker_check_applies_the_same_limit_to_a_probe():
    probe = SourceProbe(width=3840, height=2160, duration_seconds=301)

    with pytest.raises(ValueError, match="the limit is 5 minutes"):
        validate_source(probe, max_duration_seconds=300)
    assert validate_source(probe, max_duration_seconds=600) is None


# --- the height an edit starts from ----------------------------------------


def test_an_upload_is_edited_from_its_mp4_so_its_height_is_capped():
    assert editable_height(height=2160, is_edit=False, mp4_max_height=720) == 720
    assert editable_height(height=2160, is_edit=False, mp4_max_height=1080) == 1080
    assert editable_height(height=480, is_edit=False, mp4_max_height=720) == 480


def test_an_edit_jobs_stored_height_already_describes_its_own_output():
    assert editable_height(height=1440, is_edit=True, mp4_max_height=720) == 1440


@pytest.mark.parametrize("height", [None, 0])
def test_an_unprobed_job_has_no_editable_height(height):
    assert editable_height(height=height, is_edit=False, mp4_max_height=720) is None


# --- scale options and rules -----------------------------------------------


def test_a_720p_video_is_offered_every_rung_below_and_above_it():
    assert scale_options(720) == {"downscale": [480, 360, 240], "upscale": [1080, 1440, 2160]}


def test_a_2160p_video_is_offered_no_upscale():
    assert scale_options(2160)["upscale"] == []


def test_a_240p_video_is_offered_no_downscale():
    assert scale_options(240)["downscale"] == []


def test_a_video_between_rungs_is_offered_the_rungs_either_side():
    """A 1920-wide 800p video: 720p is smaller, 1080p is larger."""
    assert scale_options(800) == {
        "downscale": [720, 480, 360, 240],
        "upscale": [1080, 1440, 2160],
    }


def test_no_height_means_no_options_rather_than_every_option():
    assert scale_options(None) is None


@pytest.mark.parametrize("input_height", [144, 240, 360, 406, 720, 1080, 1920, 2160, 3840])
def test_every_option_offered_is_accepted_and_every_other_rung_is_refused(input_height):
    """The page builds its dropdowns from `scale_options`; the API enforces
    `check_edit_operations`. They must agree, or the editor offers something
    the API refuses.
    """
    options = scale_options(input_height)
    for operation in ("downscale", "upscale"):
        for rung in SCALE_HEIGHTS:
            request = [(operation, {"height": rung})]
            if rung in options[operation]:
                check_edit_operations(request, input_height=input_height)
            else:
                with pytest.raises(MediaRuleError):
                    check_edit_operations(request, input_height=input_height)


def test_downscale_to_the_same_height_is_refused():
    with pytest.raises(MediaRuleError, match="below the video's 720p"):
        check_edit_operations([("downscale", {"height": 720})], input_height=720)


def test_upscale_to_a_smaller_height_is_refused():
    with pytest.raises(MediaRuleError, match="above the video's 1080p"):
        check_edit_operations([("upscale", {"height": 720})], input_height=1080)


@pytest.mark.parametrize("height", [481, 1000, 4320])
def test_a_height_that_is_not_a_rung_is_refused_even_without_dimensions(height):
    """An odd height is one libx264 refuses mid-encode; 4320 is past the ladder."""
    with pytest.raises(MediaRuleError, match="must be one of"):
        check_edit_operations([("upscale", {"height": height})], input_height=None)


def test_with_no_known_height_a_real_rung_is_left_to_the_worker():
    assert check_edit_operations([("downscale", {"height": 480})], input_height=None) is None


def test_scale_is_judged_against_the_crop_it_follows_not_the_whole_frame():
    """The worker crops before it scales: 1280x720 cropped out of a 2160p
    frame and then "downscaled" to 1080p would really be an upscale.
    """
    crop = ("crop", {"x": 0, "y": 0, "w": 1280, "h": 720})

    with pytest.raises(MediaRuleError, match="below the video's 720p"):
        check_edit_operations([crop, ("downscale", {"height": 1080})], input_height=2160)
    assert check_edit_operations([crop, ("upscale", {"height": 1080})], input_height=2160) is None


def test_a_malformed_crop_leaves_its_report_to_the_crop_validator():
    """Scale then falls back to the rung check; the crop's own message wins."""
    bad_crop = ("crop", {"x": 0, "y": 0, "w": 10, "h": "tall"})

    request = [bad_crop, ("upscale", {"height": 1080})]

    assert check_edit_operations(request, input_height=720) is None


@pytest.mark.parametrize("operation", ["crop", "downscale", "upscale"])
def test_mp3_cannot_be_combined_with_anything_that_changes_the_picture(operation):
    params = {"x": 0, "y": 0, "w": 10, "h": 10} if operation == "crop" else {"height": 360}
    request = [(operation, params), ("convert", {"format": "mp3"})]

    with pytest.raises(MediaRuleError, match="cannot be combined with MP3"):
        check_edit_operations(request, input_height=720)


def test_mp3_with_a_clip_is_the_supported_audio_case():
    request = [("clip", {"start": 0, "end": 5}), ("convert", {"format": "mp3"})]
    assert check_edit_operations(request, input_height=720) is None


def test_a_height_that_is_not_an_integer_is_refused():
    with pytest.raises(MediaRuleError, match="whole number"):
        check_edit_operations([("downscale", {"height": 480.0})], input_height=720)


def test_the_top_rung_is_the_top_of_the_ladder():
    assert MAX_SCALE_HEIGHT == 2160
    with pytest.raises(MediaRuleError):
        check_scale("upscale", 4320, 2160)


def test_the_worker_applies_the_rules_to_the_real_input_height():
    """An MP4 encoded under an older cap is shorter than the stored height says."""
    probe = SourceProbe(width=1280, height=720, duration_seconds=10)

    with pytest.raises(ValueError, match="below the video's 720p"):
        validate_edit_rules([("downscale", {"height": 1080})], probe)
    assert validate_edit_rules([("downscale", {"height": 480})], probe) is None
