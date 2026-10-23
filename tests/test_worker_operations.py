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
        {"operation": "crop", "params": {"x": 10, "y": 20, "w": 1280, "h": 720}},
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
    assert command[command.index("-vf") + 1] == (
        "fps=fps=min(30\\,source_fps),crop=1280:720:10:20,scale=-2:480,format=yuv420p"
    )
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


def _probes(*, source, output):
    """A prober that tells the source from the output, as ffprobe would.

    One answer for both made these tests depend on the edit never probing its
    source -- and source-aware rules (a downscale must go below the source,
    a corrupt source fails at once) rightly probe it.
    """
    probed: list[str] = []

    def prober(path):
        probed.append(path.name)
        answer = source if path.name == "source" else output
        if isinstance(answer, Exception):
            raise answer
        return answer

    return prober, probed


def test_an_edit_reports_its_own_size_and_asks_for_a_ladder():
    """A 4K source downscaled to 720p must get a 720p-topped ladder, so the
    size stored is the output's, not the source's.
    """
    prober, probed = _probes(
        source=SourceProbe(3840, 2160, 8.0), output=SourceProbe(1280, 720, 8.0)
    )

    result = _edit([{"operation": "downscale", "params": {"height": 720}}], prober=prober)

    assert probed[-1] == "output.mp4", "the size reported is probed from the output"
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
    prober, _ = _probes(
        source=SourceProbe(3840, 2160, 8.0), output=RuntimeError("ffprobe failed")
    )

    result = _edit([{"operation": "downscale", "params": {"height": 720}}], prober=prober)

    assert result.ladder_pending is False
    assert result.height is None


# --- an edit gets a poster frame too, taken from what the edit produced ------


def _edit_with_thumbnails(operations, *, thumbnailer, output=None):
    uploads: list[tuple[str, str]] = []

    class Store(_LadderStore):
        def upload_file(self, *, key, source, content_type="video/mp4"):
            uploads.append((key, content_type))

    def runner(command, **_kwargs):
        Path(command[-1]).write_bytes(b"out")
        return SimpleNamespace(returncode=0, stderr="")

    prober, _ = _probes(
        source=SourceProbe(1920, 1080, 60.0), output=output or SourceProbe(640, 480, 60.0)
    )
    job_id = uuid4()
    result = EditProcessor(
        Store(), operations, output_prefix="outputs", runner=runner, prober=prober,
        thumbnailer=thumbnailer,
    ).run(job_id=job_id, source_key="uploads/a/demo.mp4")
    return job_id, result, uploads


def test_an_edit_gets_its_own_poster_frame_from_the_edited_video():
    """Every edit's library card was blank (23 Oct 2026): uploads took a
    frame, edits never did. Taken from the output, so it shows the crop."""
    seen = []

    def thumbnailer(source_path, output_path, probe):
        seen.append((source_path.name, probe.width, probe.height))
        output_path.write_bytes(b"jpeg")
        return output_path

    job_id, result, uploads = _edit_with_thumbnails(
        [
            {"operation": "crop", "params": {"x": 240, "y": 0, "w": 1440, "h": 1080}},
            {"operation": "downscale", "params": {"height": 480}},
            {"operation": "convert", "params": {"format": "mkv"}},
        ],
        thumbnailer=thumbnailer,
    )

    assert seen == [("output.mkv", 640, 480)]
    assert result.thumbnail_key == f"outputs/{job_id}/thumbnail.jpg"
    assert (result.thumbnail_key, "image/jpeg") in uploads


def test_a_poster_frame_that_fails_never_fails_the_edit():
    def thumbnailer(*_args):
        raise RuntimeError("ffmpeg could not seek")

    _, result, _ = _edit_with_thumbnails(
        [{"operation": "downscale", "params": {"height": 480}}], thumbnailer=thumbnailer
    )

    assert result.thumbnail_key is None
    assert result.ladder_pending is True, "the edit itself is done"


def test_an_mp3_edit_gets_no_poster_frame():
    def thumbnailer(*_args):
        raise AssertionError("an MP3 has no picture")

    _, result, _ = _edit_with_thumbnails(
        [
            {"operation": "clip", "params": {"start": 0, "end": 1}},
            {"operation": "convert", "params": {"format": "mp3"}},
        ],
        thumbnailer=thumbnailer,
    )

    assert result.thumbnail_key is None
