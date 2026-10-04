from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

import pytest

from app.worker.operations import EditProcessor
from app.worker.probe import SourceProbe
from app.worker.storage import ObjectStoreError


class FakeStore:
    def __init__(self):
        self.uploads = []

    def object_exists(self, _key):
        return True

    def download_file(self, *, key, destination):
        Path(destination).write_bytes(b"source")

    def upload_file(self, *, key, source, content_type="video/mp4"):
        self.uploads.append((key, source, content_type))


def test_edit_runs_one_ffmpeg_command_in_fixed_order_and_uploads_the_right_type():
    commands = []

    def runner(command, **_kwargs):
        commands.append(command)
        Path(command[-1]).write_bytes(b"result")
        return SimpleNamespace(returncode=0, stderr="")

    operations = [
        {"operation": "convert", "params": {"format": "mkv"}},
        {"operation": "downscale", "params": {"height": 480}},
        {"operation": "crop", "params": {"x": 10, "y": 20, "w": 640, "h": 360}},
        {"operation": "clip", "params": {"start": 1, "end": 5}},
    ]
    store = FakeStore()
    processor = EditProcessor(
        store,
        operations,
        output_prefix="outputs",
        runner=runner,
        prober=lambda _path: SourceProbe(1920, 1080, 10),
    )

    result = processor.run(job_id=uuid4(), source_key="outputs/source/movie.mp4")

    assert len(commands) == 1
    command = commands[0]
    assert command.index("-ss") < command.index("-i")
    assert command[command.index("-vf") + 1] == "crop=640:360:10:20,scale=-2:480"
    assert "-c:v" in command
    assert "copy" not in command
    assert result.output_key.endswith("movie.mkv")
    assert store.uploads[0][2] == "video/x-matroska"


def test_convert_only_mkv_is_a_remux():
    commands = []

    def runner(command, **_kwargs):
        commands.append(command)
        Path(command[-1]).write_bytes(b"mkv")
        return SimpleNamespace(returncode=0, stderr="")

    EditProcessor(
        FakeStore(),
        [{"operation": "convert", "params": {"format": "mkv"}}],
        output_prefix="outputs",
        runner=runner,
        prober=lambda _path: None,
    ).run(job_id=uuid4(), source_key="outputs/source/movie.mp4")

    assert commands[0][commands[0].index("-c") + 1] == "copy"
    assert "-vf" not in commands[0]


def test_source_aware_validation_happens_before_ffmpeg():
    calls = []
    processor = EditProcessor(
        FakeStore(),
        [{"operation": "crop", "params": {"x": 0, "y": 0, "w": 1921, "h": 1080}}],
        output_prefix="outputs",
        runner=lambda *_args, **_kwargs: calls.append("ffmpeg"),
        prober=lambda _path: SourceProbe(1920, 1080, 10),
    )

    with pytest.raises(ObjectStoreError) as caught:
        processor.run(job_id=uuid4(), source_key="outputs/source/movie.mp4")

    assert calls == []
    assert caught.value.user_message == "crop must fit within the source video's frame"


def test_clip_to_mp3_uses_audio_encoding_and_the_mp3_content_type():
    commands = []
    store = FakeStore()

    def runner(command, **_kwargs):
        commands.append(command)
        Path(command[-1]).write_bytes(b"mp3")
        return SimpleNamespace(returncode=0, stderr="")

    processor = EditProcessor(
        store,
        [
            {"operation": "convert", "params": {"format": "mp3"}},
            {"operation": "clip", "params": {"start": 0, "end": 5}},
        ],
        output_prefix="outputs",
        runner=runner,
        prober=lambda _path: SourceProbe(1920, 1080, 10),
    )

    result = processor.run(job_id=uuid4(), source_key="outputs/source/movie.mp4")

    assert "-vn" in commands[0]
    assert "libmp3lame" in commands[0]
    assert result.output_key.endswith(".mp3")
    assert store.uploads[0][2] == "audio/mpeg"


# --- an edit gets a ladder too, sized from what the edit produced -----------


def _edit(operations, *, prober):
    from app.worker.operations import EditProcessor

    calls: list[list[str]] = []

    def runner(command, **_kwargs):
        calls.append(command)
        Path(command[-1]).write_bytes(b"out")
        return SimpleNamespace(returncode=0, stderr="")

    processor = EditProcessor(
        _LadderStore(), operations, output_prefix="outputs", runner=runner, prober=prober
    )
    return processor.run(job_id=uuid4(), source_key="uploads/a/demo.mp4")


class _LadderStore:
    def object_exists(self, key):
        return True

    def download_file(self, *, key, destination):
        Path(destination).write_bytes(b"src")

    def upload_file(self, *, key, source, content_type="video/mp4"):
        pass


def test_an_edit_reports_its_own_size_and_asks_for_a_ladder():
    """A 4K source downscaled to 720p must get a 720p-topped ladder, so the
    size probed is the output's, not the source's.
    """
    probed: list[str] = []

    def prober(path):
        probed.append(path.name)
        return SourceProbe(1280, 720, 8.0)

    result = _edit([{"operation": "downscale", "params": {"height": 720}}], prober=prober)

    assert probed == ["output.mp4"]
    assert result.ladder_pending is True
    assert (result.width, result.height, result.duration_seconds) == (1280, 720, 8.0)


def test_an_mp3_edit_gets_no_ladder_and_is_never_probed():
    def prober(path):
        raise AssertionError("an MP3 has no picture to probe")

    result = _edit(
        [
            {"operation": "clip", "params": {"start": 0, "end": 1}},
            {"operation": "convert", "params": {"format": "mp3"}},
        ],
        prober=lambda path: SourceProbe(1920, 1080, 10)
        if path.name == "source"
        else prober(path),
    )

    assert result.ladder_pending is False
    assert result.output_key.endswith(".mp3")


def test_an_unreadable_edit_output_still_finishes_without_a_ladder():
    def prober(_path):
        raise RuntimeError("ffprobe failed")

    result = _edit([{"operation": "downscale", "params": {"height": 720}}], prober=prober)

    assert result.ladder_pending is False
    assert result.height is None
