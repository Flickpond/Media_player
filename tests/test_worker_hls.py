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


def test_a_4k_source_gets_every_rung_up_to_its_own_height_but_480():
    """1440p stays; 480p is a full encode a player barely uses between 360p
    and 720p, so it only appears as a video's top rung.
    """
    assert variants_for(2160) == (360, 720, 1080, 1440, 2160)


def test_a_1080p_source_stops_at_1080():
    assert variants_for(1080) == (360, 720, 1080)


def test_a_720p_source_stops_at_720():
    assert variants_for(720) == (360, 720)


def test_the_host_cap_limits_a_4k_source():
    """The current server cannot encode a 4K ladder inside the job timeout;
    capping there is what keeps a 4K upload from failing its ladder outright.
    """
    assert variants_for(2160, max_height=1080) == (360, 720, 1080)


def test_the_cap_never_raises_a_source_above_its_own_height():
    assert variants_for(720, max_height=2160) == (360, 720)


def test_a_source_taller_than_every_rung_tops_out_at_2160():
    """A 9:16 4K phone video is 3840 tall: it gets 2160, not a 3840 rung."""
    assert variants_for(3840)[-1] == 2160


def test_every_rung_has_its_own_bitrate():
    from app.worker.hls import BITRATES, LADDER_HEIGHTS

    assert set(LADDER_HEIGHTS) <= set(BITRATES)


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
    """These have to describe the same number of renditions. A mismatch is
    an FFmpeg error rather than a quietly wrong output, which is the good
    case -- but only because this is built from the list rather than
    hardcoded.
    """
    command = build_ladder_command(SOURCE, Path("/tmp/out"), heights=(360, 480))

    filter_complex = command[command.index("-filter_complex") + 1]
    assert "split=2" in filter_complex
    assert filter_complex.count("scale=-2:") == 2

    var_stream_map = command[command.index("-var_stream_map") + 1]
    assert var_stream_map == "v:0,agroup:audio v:1,agroup:audio a:0,agroup:audio"
    assert command.count("-map") == 3  # two video, and the audio once


def test_the_audio_is_encoded_once_for_every_rung():
    """One soundtrack, shared through an HLS audio group, instead of the same
    AAC encode repeated per rung."""
    command = build_ladder_command(SOURCE, Path("/tmp/out"), heights=(360, 720, 1080, 2160))

    assert command.count("a:0?") == 1
    var_stream_map = command[command.index("-var_stream_map") + 1]
    assert var_stream_map.count("agroup:audio") == 5  # four rungs + the audio itself
    assert var_stream_map.endswith("a:0,agroup:audio")


def test_the_frame_rate_is_capped_before_anything_is_scaled():
    """Dropping frames after scaling them would have paid for the scaling."""
    command = build_ladder_command(SOURCE, Path("/tmp/out"), heights=(360, 720))
    filter_complex = command[command.index("-filter_complex") + 1]

    assert filter_complex.startswith("[0:v]fps=fps=min(30\\,source_fps),split=")


def test_rungs_below_1080_are_scaled_from_the_1080_rendition():
    command = build_ladder_command(SOURCE, Path("/tmp/out"), heights=(360, 720, 1080, 2160))
    graph = dict(
        part.split("scale=-2:", 1)
        for part in command[command.index("-filter_complex") + 1].split(";")[1:]
    )

    # The source feeds 1080 and 2160; 1080's output feeds 720 and 360.
    assert graph["[s1080]"].startswith("1080,split=3[v2out][a360][a720]")
    assert graph["[s2160]"] == "2160[v3out]"
    assert graph["[a720]"] == "720[v1out]"
    assert graph["[a360]"] == "360[v0out]"


def test_without_a_1080_rung_every_rung_comes_from_the_source():
    command = build_ladder_command(SOURCE, Path("/tmp/out"), heights=(360, 720))
    filter_complex = command[command.index("-filter_complex") + 1]

    assert "split=2[s360][s720]" in filter_complex
    assert "[a" not in filter_complex


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


def test_a_single_rung_still_produces_a_valid_stream_map():
    command = build_ladder_command(SOURCE, Path("/tmp/out"), heights=(240,))
    assert command[command.index("-var_stream_map") + 1] == "v:0,agroup:audio a:0,agroup:audio"


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


def test_each_rung_encodes_to_a_quality_capped_at_its_bitrate():
    """Capped quality, not a fixed rate: simple footage comes out smaller, and
    complex footage still never passes the ceiling a player plans around --
    the fixed rate let a "5 Mbps" 1080p rung peak at 10 Mbps on grainy video.
    """
    from app.worker.hls import BITRATES, LADDER_CRF

    command = build_ladder_command(SOURCE, Path("/tmp/out"), heights=(360, 1080))

    def option(name):
        return command[command.index(name) + 1]

    assert "-b:v:0" not in command, "a fixed bitrate would undo the saving"
    assert option("-crf:v:1") == str(LADDER_CRF)
    assert option("-maxrate:v:1") == BITRATES[1080] == "5000k"
    assert option("-bufsize:v:1") == "10000k", "two seconds of the ceiling"
    assert option("-maxrate:v:0") == BITRATES[360]
