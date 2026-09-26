import pytest
from pydantic import ValidationError

from app.schemas.edit import EditRequest


def test_request_accepts_every_operation_in_any_client_order():
    request = EditRequest.model_validate(
        {
            "operations": [
                {"operation": "convert", "params": {"format": "mkv"}},
                {"operation": "crop", "params": {"x": 0, "y": 1, "w": 100, "h": 50}},
                {"operation": "clip", "params": {"start": 1, "end": 2.5}},
                {"operation": "downscale", "params": {"height": 480}},
            ]
        }
    )

    assert [item.operation for item in request.operations] == [
        "convert",
        "crop",
        "clip",
        "downscale",
    ]


@pytest.mark.parametrize(
    "payload",
    [
        {"operations": []},
        {"operations": [{"operation": "rotate", "params": {}}]},
        {
            "operations": [
                {"operation": "downscale", "params": {"height": 480}},
                {"operation": "upscale", "params": {"height": 1080}},
            ]
        },
        {
            "operations": [
                {"operation": "convert", "params": {"format": "mkv"}},
                {"operation": "convert", "params": {"format": "mp3"}},
            ]
        },
        {"operations": [{"operation": "crop", "params": {"x": True, "y": 0, "w": 1, "h": 1}}]},
        {"operations": [{"operation": "downscale", "params": {"height": "480"}}]},
        {"operations": [{"operation": "convert", "params": {"format": "avi"}}]},
    ],
)
def test_request_rejects_ambiguous_or_coerced_operations(payload):
    with pytest.raises(ValidationError):
        EditRequest.model_validate(payload)


def test_stored_operations_are_plain_json_values():
    request = EditRequest.model_validate(
        {"operations": [{"operation": "clip", "params": {"start": 0, "end": 1.5}}]}
    )

    assert request.stored_operations() == [
        {"operation": "clip", "params": {"start": 0, "end": 1.5}}
    ]
