import subprocess
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

import pytest

from app.worker.hls import (
    MASTER_PLAYLIST_NAME,
    HlsLadderBuilder,
    build_ladder_command,
    content_type_for,
    variants_for,
)
from app.worker.probe import SourceProbe
from app.worker.storage import UNPROCESSABLE_VIDEO, ObjectStoreError

SOURCE = Path("/tmp/source.mp4")


def probe(width: int = 1920, height: int = 1080, duration: float = 30.0) -> SourceProbe:
    return SourceProbe(width=width, height=height, duration_seconds=duration)


class FakeStore:
    def __init__(self) -> None:
        self.uploads: list[tuple[str, str]] = []

    def object_exists(self, key: str) -> bool:
        return True

    def download_file(self, *, key: str, destination: str) -> None: ...

    def upload_file(self, *, key: str, source: str, content_type: str = "video/mp4") -> None:
        self.uploads.append((key, content_type))


def ladder_writing_runner(*, variant_count: int = 3, returncode: int = 0, stderr: str = ""):
    """Writes the files a real FFmpeg run would leave behind.

    The output pattern is the last argument, so the working directory is
    two levels up from it -- the same expansion FFmpeg does with %v.
    """

    def runner(command, **_kwargs):
        runner.command = command
        if returncode == 0:
            work_dir = Path(command[-1]).parent.parent
            (work_dir / MASTER_PLAYLIST_NAME).write_text("#EXTM3U\n")
            for index in range(variant_count):
                variant = work_dir / f"v{index}"
                variant.mkdir(parents=True, exist_ok=True)
                (variant / "index.m3u8").write_text("#EXTM3U\n")
                (variant / "seg00001.ts").write_bytes(b"\x47")
        return SimpleNamespace(returncode=returncode, stderr=stderr, stdout="")

    runner.command = []
    return runner


# --- which rungs a source gets -------------------------------------------


def test_a_1080p_source_gets_all_three_rungs():
    assert variants_for(1080) == (360, 480, 720)


def test_a_720p_source_gets_all_three_rungs():
    assert variants_for(720) == (360, 480, 720)


def test_a_480p_source_is_never_given_a_720p_rendition():
    """The rule the whole ladder exists to respect: a rendition taller than
    the source costs encode time and storage to carry no extra detail.
    """
    assert variants_for(480) == (360, 480)


def test_an_in_between_height_only_gets_the_rungs_below_it():
    assert variants_for(600) == (360, 480)


def test_a_360p_source_gets_exactly_one_rendition():
    assert variants_for(360) == (360,)


def test_a_source_shorter_than_every_rung_gets_one_at_its_own_height():
    """A 240p upload must still get a playable ladder, and must still not be
    upscaled -- so the rung is the source's own height, not the lowest rung.
    """
    assert variants_for(240) == (240,)


# --- the command ----------------------------------------------------------


def test_split_count_and_var_stream_map_agree_with_the_variant_list():
    """These three have to describe the same number of renditions. A
    mismatch is an FFmpeg error rather than a quietly wrong output, which is
    the good case -- but only because this is built from the list rather
    than hardcoded at three.
    """
    command = build_ladder_command(SOURCE, Path("/tmp/out"), heights=(360, 480))

    filter_complex = command[command.index("-filter_complex") + 1]
    assert "split=2" in filter_complex
    assert filter_complex.count("scale=-2:") == 2

    var_stream_map = command[command.index("-var_stream_map") + 1]
    assert var_stream_map == "v:0,a:0 v:1,a:1"
    assert command.count("-map") == 4  # two video, two audio


def test_each_rendition_is_scaled_to_its_own_height():
    command = build_ladder_command(SOURCE, Path("/tmp/out"), heights=(360, 480, 720))
    filter_complex = command[command.index("-filter_complex") + 1]

    for height in (360, 480, 720):
        assert f"scale=-2:{height}" in filter_complex


def test_the_master_playlist_is_named():
    command = build_ladder_command(SOURCE, Path("/tmp/out"), heights=(360,))
    assert command[command.index("-master_pl_name") + 1] == MASTER_PLAYLIST_NAME


def test_audio_is_mapped_optionally_so_a_silent_source_does_not_fail_here():
    command = build_ladder_command(SOURCE, Path("/tmp/out"), heights=(360,))
    assert "a:0?" in command


def test_a_single_rung_still_produces_a_valid_two_stream_map():
    command = build_ladder_command(SOURCE, Path("/tmp/out"), heights=(240,))
    assert command[command.index("-var_stream_map") + 1] == "v:0,a:0"


# --- content types --------------------------------------------------------


def test_playlists_and_segments_get_different_content_types():
    """MinIO serves back whatever it was told. A manifest handed over as
    video/mp4 is a bug that looks fine in storage and only shows up as a
    player refusing to load it.
    """
    assert content_type_for("master.m3u8") == "application/vnd.apple.mpegurl"
    assert content_type_for("index.m3u8") == "application/vnd.apple.mpegurl"
    assert content_type_for("seg00001.ts") == "video/mp2t"


# --- building and uploading ----------------------------------------------


def test_build_uploads_every_part_and_returns_the_master_key():
    store = FakeStore()
    builder = HlsLadderBuilder(
        store, output_prefix="outputs", runner=ladder_writing_runner(variant_count=3)
    )
    job_id = uuid4()

    hls_key = builder.build(job_id=job_id, source_path=SOURCE, probe=probe())

    assert hls_key == f"outputs/{job_id}/hls/{MASTER_PLAYLIST_NAME}"
    uploaded = {key for key, _ in store.uploads}
    assert f"outputs/{job_id}/hls/master.m3u8" in uploaded
    assert f"outputs/{job_id}/hls/v0/index.m3u8" in uploaded
    assert f"outputs/{job_id}/hls/v2/seg00001.ts" in uploaded


def test_every_uploaded_part_carries_the_right_content_type():
    store = FakeStore()
    builder = HlsLadderBuilder(
        store, output_prefix="outputs", runner=ladder_writing_runner(variant_count=1)
    )

    builder.build(job_id=uuid4(), source_path=SOURCE, probe=probe(height=360))

    for key, content_type in store.uploads:
        expected = "application/vnd.apple.mpegurl" if key.endswith(".m3u8") else "video/mp2t"
        assert content_type == expected, key


def test_the_source_height_decides_how_many_renditions_are_built():
    store = FakeStore()
    runner = ladder_writing_runner(variant_count=2)
    builder = HlsLadderBuilder(store, output_prefix="outputs", runner=runner)

    builder.build(job_id=uuid4(), source_path=SOURCE, probe=probe(width=854, height=480))

    assert "split=2" in runner.command[runner.command.index("-filter_complex") + 1]


def test_a_failing_ffmpeg_is_reported_with_its_stderr_kept_out_of_the_user_message():
    store = FakeStore()
    builder = HlsLadderBuilder(
        store,
        output_prefix="outputs",
        runner=ladder_writing_runner(returncode=1, stderr="Stream map 'a:0' matches no streams"),
    )

    with pytest.raises(ObjectStoreError, match="matches no streams") as caught:
        builder.build(job_id=uuid4(), source_path=SOURCE, probe=probe())

    assert caught.value.user_message == UNPROCESSABLE_VIDEO
    assert "Stream map" not in caught.value.user_message


def test_a_run_that_produces_no_master_playlist_is_refused():
    store = FakeStore()

    def runner(command, **_kwargs):
        return SimpleNamespace(returncode=0, stderr="", stdout="")

    builder = HlsLadderBuilder(store, output_prefix="outputs", runner=runner)

    with pytest.raises(ObjectStoreError, match="no master playlist"):
        builder.build(job_id=uuid4(), source_path=SOURCE, probe=probe())

    assert store.uploads == []


def test_a_timeout_is_reported_rather_than_raised_raw():
    def runner(_command, **_kwargs):
        raise subprocess.TimeoutExpired(cmd="ffmpeg", timeout=870)

    builder = HlsLadderBuilder(FakeStore(), output_prefix="outputs", runner=runner)

    with pytest.raises(ObjectStoreError, match="timed out") as caught:
        builder.build(job_id=uuid4(), source_path=SOURCE, probe=probe())

    assert caught.value.user_message == UNPROCESSABLE_VIDEO
