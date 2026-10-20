import subprocess
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

import pytest

from app.worker import after_ladder, preview
from app.worker.__main__ import register_hooks
from app.worker.preview import (
    PreviewError,
    PreviewSprite,
    build_sprite_command,
    build_vtt,
    plan_sprite,
    preview_prefix_for,
)
from app.worker.probe import SourceProbe


def _layout(width=1920, height=1080, duration=12.5, interval=5, tile_width=160):
    return plan_sprite(
        width=width,
        height=height,
        duration_seconds=duration,
        interval_seconds=interval,
        tile_width=tile_width,
    )


# --- the layout ---------------------------------------------------------------


def test_a_short_video_gets_one_frame_per_started_interval_on_a_single_row():
    layout = _layout(duration=12.5)

    assert (layout.interval_seconds, layout.frames) == (5, 3)
    assert (layout.columns, layout.rows) == (3, 1)
    assert (layout.tile_width, layout.tile_height) == (160, 90)


def test_frames_wrap_onto_rows_of_ten():
    layout = _layout(duration=120)

    assert (layout.frames, layout.columns, layout.rows) == (24, 10, 3)


def test_a_portrait_video_keeps_its_shape_at_an_even_height():
    assert _layout(width=1080, height=1920).tile_height == 284


def test_a_long_video_widens_the_interval_instead_of_growing_the_sheet():
    layout = _layout(duration=3600)

    assert layout.interval_seconds == 18
    assert layout.frames == 200
    assert (layout.columns, layout.rows) == (10, 20)


@pytest.mark.parametrize(
    ("width", "height", "duration"), [(0, 1080, 10), (1920, 0, 10), (1920, 1080, 0)]
)
def test_nothing_to_preview_is_refused_rather_than_guessed(width, height, duration):
    with pytest.raises(PreviewError):
        _layout(width=width, height=height, duration=duration)


def test_the_preview_lives_beside_the_mp4():
    assert preview_prefix_for("outputs/abc/demo.mp4") == "outputs/abc/preview/"


# --- the FFmpeg command -------------------------------------------------------


def test_frames_are_picked_before_they_are_scaled_and_tiled_into_one_image():
    command = build_sprite_command(
        source_path=Path("in.mp4"), output_path=Path("out.jpg"), layout=_layout(duration=120)
    )
    filters = command[command.index("-vf") + 1]

    assert filters == "fps=1/5,scale=160:90,setsar=1,tile=10x3"
    assert command[command.index("-frames:v") + 1] == "1"
    assert "-an" in command
    assert command[-1] == str(Path("out.jpg"))


# --- the index ----------------------------------------------------------------


def test_each_cue_names_its_own_rectangle_of_the_sheet():
    vtt = build_vtt(_layout(duration=120), 120)
    lines = vtt.split("\n")

    assert lines[:4] == [
        "WEBVTT",
        "",
        "00:00:00.000 --> 00:00:05.000",
        "sprite.jpg#xywh=0,0,160,90",
    ]
    # The eleventh frame starts the second row.
    assert "00:00:50.000 --> 00:00:55.000\nsprite.jpg#xywh=0,90,160,90" in vtt
    assert "00:01:55.000 --> 00:02:00.000\nsprite.jpg#xywh=480,180,160,90" in vtt


def test_the_last_cue_ends_where_the_video_does():
    vtt = build_vtt(_layout(duration=12.5), 12.5)

    assert vtt.rstrip().endswith("00:00:10.000 --> 00:00:12.500\nsprite.jpg#xywh=320,0,160,90")


def test_cue_times_past_an_hour_carry_the_hours():
    layout = _layout(duration=3700)

    assert layout.interval_seconds == 19
    assert "01:00:10.000 --> 01:00:29.000" in build_vtt(layout, 3700)


# --- making and storing it ----------------------------------------------------


class FakeStore:
    def __init__(self, *, fail_upload_of: str | None = None) -> None:
        self.downloaded: list[str] = []
        self.uploaded: list[tuple[str, str, bytes]] = []
        self.deleted_prefixes: list[str] = []
        self._fail_upload_of = fail_upload_of

    def download_file(self, *, key: str, destination: str) -> None:
        self.downloaded.append(key)
        Path(destination).write_bytes(b"mp4")

    def upload_file(self, *, key: str, source: str, content_type: str = "video/mp4") -> None:
        if self._fail_upload_of and key.endswith(self._fail_upload_of):
            raise RuntimeError("bucket unreachable")
        self.uploaded.append((key, content_type, Path(source).read_bytes()))

    def delete_prefix(self, prefix: str) -> None:
        self.deleted_prefixes.append(prefix)


def _runner(*, returncode=0, write=True, raises=None):
    def runner(command, **kwargs):
        runner.command, runner.kwargs = command, kwargs
        if raises is not None:
            raise raises
        if write:
            Path(command[-1]).write_bytes(b"\xff\xd8sheet")
        return SimpleNamespace(returncode=returncode, stderr="decoder error")

    return runner


def _sprite(store, runner, probe=None, record=None):
    probe = probe or SourceProbe(width=1920, height=1080, duration_seconds=12.5)
    recorded = []

    def remember(job_id, key):
        recorded.append((job_id, key))
        return True

    sprite = PreviewSprite(
        store,
        prober=lambda _path: probe,
        record=record or remember,
        runner=runner,
        timeout_seconds=42,
    )
    sprite.recorded = recorded
    return sprite


def test_the_sheet_and_then_its_index_are_stored_beside_the_mp4_and_recorded():
    store = FakeStore()
    runner = _runner()
    job_id = uuid4()
    sprite = _sprite(store, runner)

    key = sprite.run(job_id, "outputs/abc/demo.mp4")

    assert key == "outputs/abc/preview/sprite.vtt"
    assert store.downloaded == ["outputs/abc/demo.mp4"]
    assert [(k, t) for k, t, _ in store.uploaded] == [
        ("outputs/abc/preview/sprite.jpg", "image/jpeg"),
        ("outputs/abc/preview/sprite.vtt", "text/vtt"),
    ]
    assert store.uploaded[1][2].startswith(b"WEBVTT\n\n00:00:00.000 --> 00:00:05.000")
    assert sprite.recorded == [(job_id, "outputs/abc/preview/sprite.vtt")]
    assert store.deleted_prefixes == []
    assert runner.kwargs["timeout"] == 42


@pytest.mark.parametrize(
    ("runner", "message"),
    [
        (_runner(returncode=1), "decoder error"),
        (_runner(write=False), "no preview sheet"),
        (_runner(raises=subprocess.TimeoutExpired("ffmpeg", 42)), "timed out after 42s"),
    ],
)
def test_a_sheet_ffmpeg_could_not_make_stores_and_records_nothing(runner, message):
    store = FakeStore()
    sprite = _sprite(store, runner)

    with pytest.raises(PreviewError, match=message):
        sprite.run(uuid4(), "outputs/abc/demo.mp4")

    assert store.uploaded == [] and sprite.recorded == []


def test_an_index_that_could_not_be_stored_takes_its_sheet_with_it():
    store = FakeStore(fail_upload_of="sprite.vtt")
    sprite = _sprite(store, _runner())

    with pytest.raises(RuntimeError, match="bucket unreachable"):
        sprite.run(uuid4(), "outputs/abc/demo.mp4")

    assert store.deleted_prefixes == ["outputs/abc/preview/"]
    assert sprite.recorded == []


def test_a_video_deleted_while_its_preview_was_made_leaves_nothing_behind():
    """The delete saw no `preview_key`, so it removed no preview: only this
    can, once the row turns out to be gone."""
    store = FakeStore()

    key = _sprite(store, _runner(), record=lambda *_: False).run(uuid4(), "outputs/abc/demo.mp4")

    assert key is None
    assert store.deleted_prefixes == ["outputs/abc/preview/"]


def test_a_preview_the_database_would_not_take_is_removed_and_the_error_raised():
    store = FakeStore()

    def unreachable(*_):
        raise ConnectionError("database unreachable")

    with pytest.raises(ConnectionError):
        _sprite(store, _runner(), record=unreachable).run(uuid4(), "outputs/abc/demo.mp4")

    assert store.deleted_prefixes == ["outputs/abc/preview/"]


def test_recording_writes_through_the_repository_on_a_loop_of_its_own(monkeypatch):
    """The hook runs in the ladder job's worker thread, not on its loop."""
    seen = []

    class Session:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *_):
            return False

    async def fake_mark(session, job_id, *, preview_key):
        seen.append((job_id, preview_key))
        return None if preview_key.endswith("gone.vtt") else "row"

    monkeypatch.setattr(preview, "get_worker_session_factory", lambda: Session)
    monkeypatch.setattr(preview, "mark_preview_ready", fake_mark)
    job_id = uuid4()

    assert preview.record_preview(job_id, "outputs/abc/preview/sprite.vtt") is True
    assert preview.record_preview(job_id, "outputs/abc/preview/gone.vtt") is False
    assert seen[0] == (job_id, "outputs/abc/preview/sprite.vtt")


# --- the switch ---------------------------------------------------------------


@pytest.fixture
def hooks(monkeypatch):
    registered: list = []
    monkeypatch.setattr(after_ladder, "HOOKS", registered)
    return registered


def _settings(**switches):
    return SimpleNamespace(
        worker_keep_hdr=switches.get("keep_hdr", False),
        worker_preview_sprite=switches.get("preview", False),
    )


def test_with_the_switch_off_no_preview_is_made_after_the_ladder(hooks):
    register_hooks(_settings())

    assert hooks == []


def test_with_the_switch_on_every_ladder_is_followed_by_a_preview(hooks):
    register_hooks(_settings(preview=True))

    assert hooks == [preview.make_preview]


def test_the_preview_runs_after_keep_hdr_has_queued_its_rendition(hooks):
    from app.worker.renditions import queue_keep_hdr

    register_hooks(_settings(keep_hdr=True, preview=True))

    assert hooks == [queue_keep_hdr, preview.make_preview]


def test_the_hook_previews_the_jobs_mp4(monkeypatch):
    made = []
    monkeypatch.setattr(
        preview,
        "get_preview_sprite",
        lambda: SimpleNamespace(run=lambda job_id, key: made.append((job_id, key))),
    )
    job_id = uuid4()

    preview.make_preview(job_id, "outputs/abc/demo.mp4")

    assert made == [(job_id, "outputs/abc/demo.mp4")]
