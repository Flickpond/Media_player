"""HDR uploads come out as video every browser plays, with natural colours.

Modern phones record HDR by default. Untreated, the worker turned it into
10-bit H.264 ("High 10") still tagged as HDR -- which no browser plays -- and
whose picture, shown as standard range, is flat and grey. Every encoder now
tone-maps HDR to BT.709 and always writes 8-bit 4:2:0.
"""

import json
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

import pytest

from app.schemas.edit import EditRequest
from app.worker.hls import HlsLadderBuilder, build_ladder_command
from app.worker.operations import EditProcessor, build_edit_command
from app.worker.probe import SourceProbe, probe_source
from app.worker.storage import BT709_TAGS, HDR_TO_SDR, SDR_PIXELS, FfmpegProcessor, sdr_filters
from app.worker.thumbnail import build_thumbnail_command

HDR = SourceProbe(width=3840, height=2160, duration_seconds=10.0, hdr=True)
SDR = SourceProbe(width=3840, height=2160, duration_seconds=10.0)


# --- the probe recognises HDR ---------------------------------------------------


def _probe_with(transfer):
    stream = {"width": 3840, "height": 2160}
    if transfer is not None:
        stream["color_transfer"] = transfer
    stdout = json.dumps({"streams": [stream], "format": {"duration": "10.0"}})
    runner = lambda command, **_kwargs: SimpleNamespace(returncode=0, stdout=stdout, stderr="")  # noqa: E731
    return probe_source(Path("/s"), runner=runner)


@pytest.mark.parametrize("transfer", ["smpte2084", "arib-std-b67"], ids=["PQ / HDR10", "HLG"])
def test_both_hdr_transfers_are_recognised(transfer):
    assert _probe_with(transfer).hdr is True


@pytest.mark.parametrize(
    "transfer", ["bt709", "smpte170m", None], ids=["bt709", "bt601", "untagged"]
)
def test_standard_range_and_untagged_video_is_not_hdr(transfer):
    assert _probe_with(transfer).hdr is False


# --- the shared chain -------------------------------------------------------------


def test_every_chain_ends_in_8_bit_4_2_0_and_only_hdr_is_tone_mapped():
    assert sdr_filters(False) == [SDR_PIXELS]
    assert sdr_filters(True) == [HDR_TO_SDR, SDR_PIXELS]
    assert SDR_PIXELS == "format=yuv420p"


# --- each encoder ---------------------------------------------------------------------


class _Store:
    def object_exists(self, key):
        return True

    def download_file(self, *, key, destination):
        Path(destination).write_bytes(b"src")

    def upload_file(self, *, key, source, content_type="video/mp4"):
        pass


def _mp4_command(probe):
    commands = []

    def runner(command, **_kwargs):
        commands.append(command)
        Path(command[-1]).write_bytes(b"mp4")
        return SimpleNamespace(returncode=0, stderr="")

    FfmpegProcessor(_Store(), output_prefix="outputs", runner=runner, prober=lambda _p: probe).run(
        job_id=uuid4(), source_key="uploads/a/clip.mp4"
    )
    return commands[-1]


def test_an_hdr_mp4_is_tone_mapped_after_the_downscale_and_tagged_bt709():
    command = _mp4_command(HDR)
    chain = command[command.index("-vf") + 1]

    # After the scale: the same picture at a quarter of a 4K source's pixels.
    assert chain.index("scale=-2:") < chain.index(HDR_TO_SDR)
    assert chain.endswith(SDR_PIXELS)
    assert all(tag in command for tag in BT709_TAGS)


def test_a_standard_range_mp4_is_only_held_to_8_bit():
    command = _mp4_command(SDR)
    chain = command[command.index("-vf") + 1]

    assert HDR_TO_SDR not in chain and chain.endswith(SDR_PIXELS)
    assert "-color_trc" not in command


def test_an_hdr_ladder_is_tone_mapped_once_before_the_rungs_split():
    command = build_ladder_command(Path("/s"), Path("/o"), heights=(360, 1080, 2160), hdr=True)
    graph = command[command.index("-filter_complex") + 1]

    assert graph.index(HDR_TO_SDR) < graph.index("split=")
    assert graph.count(HDR_TO_SDR) == 1
    assert all(tag in command for tag in BT709_TAGS)


def test_the_ladder_asks_the_downloaded_file_whether_it_is_hdr(monkeypatch):
    """The row in the database stores size and duration, not the transfer."""
    builder = HlsLadderBuilder(_Store(), output_prefix="outputs", prober=lambda _p: HDR)
    seen = {}
    monkeypatch.setattr(builder, "build", lambda **kwargs: seen.update(kwargs) or "master")

    builder.run(job_id=uuid4(), source_key="uploads/a/clip.mp4", probe=SDR)

    assert seen["probe"].hdr is True


def test_a_ladder_whose_colour_probe_fails_is_built_as_standard_range(monkeypatch):
    def broken(_path):
        raise RuntimeError("ffprobe failed")

    builder = HlsLadderBuilder(_Store(), output_prefix="outputs", prober=broken)
    seen = {}
    monkeypatch.setattr(builder, "build", lambda **kwargs: seen.update(kwargs) or "master")

    builder.run(job_id=uuid4(), source_key="uploads/a/clip.mp4", probe=SDR)

    assert seen["probe"].hdr is False


def _edit_request(operations):
    return EditRequest.model_validate({"operations": operations})


def test_an_hdr_edit_is_tone_mapped_after_its_own_filters():
    command = build_edit_command(
        _edit_request([{"operation": "downscale", "params": {"height": 720}}]),
        ffmpeg_binary="ffmpeg",
        source_path=Path("/s"),
        output_path=Path("/o.mp4"),
        preset="veryfast",
        crf=23,
        hdr=True,
    )
    chain = command[command.index("-vf") + 1]

    assert chain.index("scale=-2:720") < chain.index(HDR_TO_SDR)
    assert chain.endswith(SDR_PIXELS) and "-color_trc" in command


def test_an_edit_with_no_validation_probe_still_asks_whether_the_source_is_hdr():
    processor = EditProcessor(
        _Store(),
        [{"operation": "convert", "params": {"format": "mp4"}}],
        output_prefix="outputs",
        prober=lambda _p: HDR,
    )

    assert processor._is_hdr(uuid4(), Path("/s"), None) is True
    assert processor._is_hdr(uuid4(), Path("/s"), SDR) is False, "the validation probe wins"


def test_an_hdr_poster_frame_is_tone_mapped_too():
    command = build_thumbnail_command(
        source_path=Path("/s"),
        output_path=Path("/t.jpg"),
        offset_seconds=1.0,
        max_width=640,
        max_height=360,
        hdr=True,
    )

    assert HDR_TO_SDR in command[command.index("-vf") + 1]
