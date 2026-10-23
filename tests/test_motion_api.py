"""Part B operations use the existing ownership and queueing API unchanged."""

import pytest

from tests.test_edit_api import edit_app as edit_app
from tests.test_edit_api import post_edit


@pytest.mark.parametrize("name,params", [
    ("rotate", {"degrees": 90}), ("rotate", {"degrees": 270}),
    ("flip", {"axis": "horizontal"}), ("speed", {"factor": 0.5}),
])
async def test_motion_edits_are_persisted_and_enqueued_from_the_original(edit_app, name, params):
    application, _, source, created, _, create_edit, enqueue, _ = edit_app
    operations = [{"operation": name, "params": params}]
    response = await post_edit(application, source.id, operations)
    assert response.status_code == 202
    assert create_edit.await_args.kwargs["source_key"] == source.source_key
    assert create_edit.await_args.kwargs["operations"] == operations
    enqueue.assert_called_once_with(created.id)


@pytest.mark.parametrize("name,params", [
    ("rotate", {"degrees": 45}), ("rotate", {"degrees": "90"}),
    ("flip", {"axis": "both"}), ("speed", {"factor": 2.25}),
    ("speed", {"factor": 1.1}), ("speed", {"factor": True}),
    ("speed", {"factor": 1, "extra": 0}),
])
async def test_invalid_motion_returns_422_before_creating_or_queueing_a_job(edit_app, name, params):
    application, _, source, _, _, create_edit, enqueue, _ = edit_app
    response = await post_edit(application, source.id, [{"operation": name, "params": params}])
    assert response.status_code == 422
    create_edit.assert_not_awaited()
    enqueue.assert_not_called()


async def test_api_scale_validation_uses_rotated_crop_dimensions(edit_app):
    application, _, source, _, _, create_edit, _, _ = edit_app
    source.width, source.height = 1920, 1080
    operations = [
        {"operation": "downscale", "params": {"height": 720}},
        {"operation": "rotate", "params": {"degrees": 90}},
        {"operation": "crop", "params": {"x": 0, "y": 0, "w": 1200, "h": 600}},
    ]
    assert (await post_edit(application, source.id, operations)).status_code == 202
    create_edit.reset_mock()
    operations[1]["params"]["degrees"] = 180
    assert (await post_edit(application, source.id, operations)).status_code == 422
    create_edit.assert_not_awaited()
