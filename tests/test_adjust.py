"""Track C, sprint 6: colour adjustments (`adjust`) through the edit seam.

Parameters (app/schemas/edit.py), rule (app/services/media_rules.py), filters
(app/worker/edit_ops/adjust.py) and the request through the API. The real
FFmpeg run is tests/integration/test_adjust_ffmpeg.py.
"""

from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock
from uuid import uuid4

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from pydantic import ValidationError

from app.api import jobs as jobs_api
from app.database import get_session
from app.main import create_app
from app.models.job import HlsStatus, Job, JobStatus
from app.schemas.edit import ADJUST_RANGES, AdjustParams, EditRequest
from app.services.media_rules import (
    OPERATION_RULES,
    Frame,
    MediaRuleError,
    check_edit_operations,
)
from app.services.output_urls import get_output_url_signer
from app.worker.edit_ops import FILTERS, filter_chain
from app.worker.operations import build_edit_command
from tests.conftest import authenticate_as


def request_with(*operations: tuple[str, dict]) -> EditRequest:
    return EditRequest.model_validate(
        {"operations": [{"operation": name, "params": params} for name, params in operations]}
    )


# --- parameters ------------------------------------------------------------


def test_any_one_field_is_a_whole_request():
    for name, value in {"brightness": 0, "contrast": 1, "saturation": 2, "gamma": 1.5}.items():
        params = AdjustParams.model_validate({name: value})
        assert params.model_dump() == {name: value}


def test_all_four_together_parse_as_sent():
    params = AdjustParams.model_validate(
        {"brightness": -0.1, "contrast": 1.25, "saturation": 0, "gamma": 2}
    )

    assert params.model_dump() == {
        "brightness": -0.1,
        "contrast": 1.25,
        "saturation": 0,
        "gamma": 2,
    }


def test_the_ends_of_every_range_are_allowed():
    for name, (low, high) in ADJUST_RANGES.items():
        AdjustParams.model_validate({name: low})
        AdjustParams.model_validate({name: high})


@pytest.mark.parametrize(
    ("name", "value"),
    [
        ("brightness", -0.31),
        ("brightness", 0.31),
        ("contrast", 0.49),
        ("contrast", 2.01),
        ("saturation", -0.01),
        ("saturation", 3.01),
        ("gamma", 0.49),
        ("gamma", 2.01),
    ],
)
def test_a_value_past_its_slider_is_refused_with_its_range(name, value):
    low, high = ADJUST_RANGES[name]

    with pytest.raises(ValidationError, match=f"{name} must be between {low:g} and {high:g}"):
        AdjustParams.model_validate({name: value})


def test_an_empty_adjust_is_refused():
    with pytest.raises(ValidationError, match="at least one of brightness"):
        AdjustParams.model_validate({})


@pytest.mark.parametrize(
    "params",
    [
        {"brightness": "0.1"},
        {"contrast": True},
        {"gamma": float("nan")},
        {"saturation": float("inf")},
        {"hue": 0.5},
    ],
)
def test_strings_booleans_non_finite_numbers_and_unknown_fields_are_refused(params):
    with pytest.raises(ValidationError):
        AdjustParams.model_validate(params)


def test_a_field_left_out_is_stored_absent_not_null():
    request = request_with(("adjust", {"saturation": 1.5}))

    assert request.stored_operations() == [
        {"operation": "adjust", "params": {"saturation": 1.5}}
    ]


def test_adjust_appears_once_like_every_operation():
    with pytest.raises(ValidationError, match="only once"):
        request_with(("adjust", {"gamma": 1.2}), ("adjust", {"gamma": 0.8}))


# --- rule ------------------------------------------------------------------


def test_colour_leaves_the_frame_as_it_is():
    rule = OPERATION_RULES["adjust"]

    assert rule.check({"contrast": 1.5}, Frame(1920, 1080)) == Frame(1920, 1080)
    assert rule.changes_picture is True


def test_colour_cannot_be_combined_with_mp3():
    with pytest.raises(MediaRuleError, match="^colour cannot be combined with MP3"):
        check_edit_operations(
            [("adjust", {"brightness": 0.1}), ("convert", {"format": "mp3"})],
            input_height=720,
        )


# --- filters ---------------------------------------------------------------


@pytest.mark.parametrize(
    ("params", "expected"),
    [
        ({"brightness": 0.1}, "eq=brightness=0.1"),
        ({"brightness": -0.3}, "eq=brightness=-0.3"),
        ({"contrast": 1.2}, "eq=contrast=1.2"),
        ({"saturation": 0}, "eq=saturation=0"),
        ({"gamma": 0.75}, "eq=gamma=0.75"),
        (
            {"gamma": 1.1, "saturation": 1.5, "contrast": 0.9, "brightness": 0.05},
            "eq=brightness=0.05:contrast=0.9:saturation=1.5:gamma=1.1",
        ),
        ({"contrast": 1.123456}, "eq=contrast=1.123"),
    ],
)
def test_the_values_become_one_eq_filter_in_its_own_option_order(params, expected):
    assert FILTERS["adjust"](params, Frame(1920, 1080)).video == (expected,)
    assert FILTERS["adjust"](params, Frame(1920, 1080)).audio == ()


def test_a_value_at_its_default_is_not_passed_and_all_defaults_add_no_filter():
    build = FILTERS["adjust"]

    assert build({"brightness": 0, "contrast": 1.3}, Frame(None, None)).video == (
        "eq=contrast=1.3",
    )
    neutral = {"brightness": 0, "contrast": 1, "saturation": 1, "gamma": 1}
    assert build(neutral, Frame(None, None)).video == ()


def test_colour_lands_after_crop_and_scale_in_the_tone_mapped_half():
    operations = [
        ("adjust", {"saturation": 1.5}),
        ("downscale", {"height": 360}),
        ("crop", {"x": 0, "y": 0, "w": 1280, "h": 720}),
    ]

    chain = filter_chain(operations, Frame(1920, 1080))

    assert chain.before_tone_map == ("crop=1280:720:0:0", "scale=-2:360")
    assert chain.after_tone_map == ("eq=saturation=1.5",)


def _command(request: EditRequest, *, hdr: bool) -> list[str]:
    return build_edit_command(
        request,
        ffmpeg_binary="ffmpeg",
        source_path=Path("/in.mp4"),
        output_path=Path("/out.mp4"),
        preset="veryfast",
        crf=23,
        hdr=hdr,
    )


def test_eq_sees_the_8_bit_picture_on_an_sdr_upload():
    command = _command(request_with(("adjust", {"contrast": 1.2})), hdr=False)
    chain = command[command.index("-vf") + 1].split(",")

    assert chain.index("format=yuv420p") < chain.index("eq=contrast=1.2")


def test_eq_runs_after_the_tone_map_on_an_hdr_upload():
    command = _command(
        request_with(("adjust", {"gamma": 1.4}), ("crop", {"x": 0, "y": 0, "w": 640, "h": 360})),
        hdr=True,
    )
    chain = command[command.index("-vf") + 1]

    assert chain.index("crop=") < chain.index("tonemap=") < chain.index("eq=gamma=1.4")


def test_a_neutral_adjust_alone_still_encodes_the_video_unchanged():
    neutral = request_with(("adjust", {"brightness": 0, "gamma": 1}))
    command = _command(neutral, hdr=False)

    assert "eq=" not in command[command.index("-vf") + 1]
    assert "libx264" in command


# --- through the API -------------------------------------------------------


def make_job() -> Job:
    now = datetime.now(UTC)
    return Job(
        id=uuid4(),
        filename="holiday.mov",
        status=JobStatus.DONE.value,
        source_key="uploads/holiday.mov",
        output_key="outputs/abc/holiday.mp4",
        operations=None,
        hls_status=HlsStatus.UNAVAILABLE.value,
        max_fps=30,
        width=1920,
        height=1080,
        duration_seconds=12.0,
        created_at=now,
        updated_at=now,
    )


class FakeSigner:
    async def create_url(self, output_key: str) -> str:
        return f"https://media.example.test/{output_key}"


@pytest_asyncio.fixture
async def api(test_user, monkeypatch):
    application = create_app()
    session = AsyncMock()
    session.execute.return_value = SimpleNamespace(
        scalars=lambda: SimpleNamespace(all=lambda: [])
    )

    async def database():
        yield session

    application.dependency_overrides[get_session] = database
    application.dependency_overrides[get_output_url_signer] = FakeSigner
    authenticate_as(application, test_user)

    job = make_job()

    async def fake_get_job(_session, _job_id, *, owner_id=None):
        return job

    create_edit = AsyncMock(return_value=Job(id=uuid4()))
    enqueue = Mock()
    monkeypatch.setattr(jobs_api, "get_job", fake_get_job)
    monkeypatch.setattr(jobs_api, "create_edit_job", create_edit)
    monkeypatch.setattr(jobs_api, "enqueue_job", enqueue)

    transport = ASGITransport(app=application)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        client.job = job
        client.create_edit = create_edit
        client.enqueue = enqueue
        yield client


async def post_edit(client, operations):
    return await client.post(f"/jobs/{client.job.id}/edit", json={"operations": operations})


async def test_an_adjust_is_queued_and_stored_as_sent(api):
    response = await post_edit(
        api, [{"operation": "adjust", "params": {"brightness": 0.1, "saturation": 1.4}}]
    )

    assert response.status_code == 202
    assert api.create_edit.await_args.kwargs["operations"] == [
        {"operation": "adjust", "params": {"brightness": 0.1, "saturation": 1.4}}
    ]
    api.enqueue.assert_called_once()


async def test_an_adjust_out_of_range_is_a_422_before_anything_is_queued(api):
    response = await post_edit(api, [{"operation": "adjust", "params": {"contrast": 5}}])

    assert response.status_code == 422
    assert "contrast must be between 0.5 and 2" in response.text
    api.create_edit.assert_not_awaited()
    api.enqueue.assert_not_called()


async def test_colour_with_mp3_is_a_422_that_names_it(api):
    response = await post_edit(
        api,
        [
            {"operation": "adjust", "params": {"gamma": 1.3}},
            {"operation": "convert", "params": {"format": "mp3"}},
        ],
    )

    assert response.status_code == 422
    assert response.json() == {"error": "colour cannot be combined with MP3 conversion"}
    api.enqueue.assert_not_called()
