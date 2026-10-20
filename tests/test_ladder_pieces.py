"""The HD ladder in pieces (app/worker/ladder_pieces.py)."""

import json
from fractions import Fraction
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

import fakeredis
import pytest

from app.worker.chunked import output_tick
from app.worker.hls import HlsLadderBuilder
from app.worker.ladder_pieces import (
    TIMELINE_START,
    LadderPieces,
    LadderPlan,
    _rates,
    audio_command,
    board_for,
    help_ladder,
    piece_archive_key,
    piece_command,
    segment_aligned_starts,
    write_master,
)
from app.worker.probe import SourceProbe

SOURCE = Path("/s")


def frames(seconds, fps):
    return [Fraction(k) / fps for k in range(int(seconds * fps))]


def plan(**overrides) -> LadderPlan:
    fields = dict(
        job_id="j", source_key="uploads/j/s.mov", heights=[360, 720, 1080], hdr=True,
        max_fps=30, preset="veryfast", output_rate="30/1",
        starts=[0.0, 30.0, 60.0], start_ticks=[0, 900, 1800], first_frames=[0, 900, 1800],
        frames=[900, 900, 0],
    )
    fields.update(overrides)
    return LadderPlan(**fields)


# --- where pieces start ---------------------------------------------------------


@pytest.mark.parametrize(
    ("fps", "rate", "length"),
    [
        (Fraction(60000, 1001), Fraction(30), 900),
        (Fraction(30000, 1001), Fraction(30000, 1001), 900),
        (Fraction(24000, 1001), Fraction(24000, 1001), 720),
        (Fraction(25), Fraction(25), 750),
    ],
)
def test_every_piece_is_whole_segments(fps, rate, length):
    """A piece ending between forced keyframes left a sliver segment whose
    bitrate made a 360p rung read as 3 Mbps in the master playlist."""
    times = frames(125, fps)
    starts = segment_aligned_starts(times, rate, 30)

    firsts = [output_tick(start, rate) for start in starts]
    assert [b - a for a, b in zip(firsts, firsts[1:], strict=False)] == [length] * (len(firsts) - 1)
    for start in starts[1:]:
        before = times[times.index(start) - 1]
        assert output_tick(before, rate) < output_tick(start, rate), "begins its output frame"


def test_a_short_remainder_joins_the_last_piece():
    starts = segment_aligned_starts(frames(70, Fraction(30)), Fraction(30), 30)
    assert [float(s) for s in starts] == [0.0, 30.0]


# --- one piece ---------------------------------------------------------------------


def test_a_piece_is_every_rung_on_the_videos_clock_placed_in_the_timeline():
    command = piece_command(
        plan(), 1, source_path=SOURCE, out_dir=Path("/o"), ffmpeg_binary="ffmpeg"
    )

    graph = command[command.index("-filter_complex") + 1]
    assert graph.startswith("[0:v]setpts=PTS-STARTPTS+900,fps=fps=30/1,setpts=PTS-STARTPTS,")
    assert "tonemap" in graph, "HDR is tone-mapped once, before the split"
    assert command[command.index("-frames:v") + 1] == "900"
    assert command[command.index("-r") + 1] == "30/1"
    assert command[command.index("-output_ts_offset") + 1] == f"{30 + TIMELINE_START:.6f}"
    assert command[command.index("-force_key_frames") + 1] == "expr:gte(t,n_forced*6)"
    assert command[command.index("-var_stream_map") + 1] == "v:0 v:1 v:2"
    assert command.count("libx264") == 3
    last = piece_command(plan(), 2, source_path=SOURCE, out_dir=Path("/o"), ffmpeg_binary="ffmpeg")
    assert "-frames:v" not in last, "the last piece runs to the end"


def test_the_audio_is_placed_where_the_video_is():
    command = audio_command(SOURCE, Path("/o/v3"), ffmpeg_binary="ffmpeg")
    assert command[command.index("-output_ts_offset") + 1] == str(TIMELINE_START)
    assert "-vn" in command


def test_a_plan_survives_the_trip_through_redis():
    assert LadderPlan.from_json(plan().to_json()) == plan()


# --- the master -----------------------------------------------------------------------


def test_the_master_has_ffmpegs_layout_so_its_readers_read_it(tmp_path):
    from app.worker.hls import label_frame_rates
    from app.worker.renditions import add_to_master, plan_rendition

    path = tmp_path / "master.m3u8"
    rungs = [("640x360", "avc1.64001e", 900_000, 400_000),
             ("1280x720", "avc1.64001f", 3_000_000, 1_300_000)]
    write_master(path, rungs, (140_000, 130_000), "v2/index.m3u8")

    master = label_frame_rates(path.read_text(), 30.0)
    assert 'URI="v2/index.m3u8"' in master
    assert ("#EXT-X-STREAM-INF:BANDWIDTH=1040000,AVERAGE-BANDWIDTH=530000,RESOLUTION=640x360,"
            'CODECS="avc1.64001e,mp4a.40.2",AUDIO="group_audio",FRAME-RATE=30.000') in master
    av1 = plan_rendition("av1", SourceProbe(1280, 720, 60.0), max_fps=30, height=720)
    extended = add_to_master(master, plan=av1, rung=720, resolution="1280x720", bandwidth=(1, 1))
    assert extended.splitlines().index("av1/720/index.m3u8") < extended.splitlines().index(
        "v0/index.m3u8"
    )


def test_a_final_sliver_does_not_set_the_peak(tmp_path):
    (tmp_path / "a.ts").write_bytes(b"x" * 600_000)
    (tmp_path / "b.ts").write_bytes(b"x" * 4_000)
    peak, average = _rates(tmp_path, [(6.0, "a.ts"), (0.03, "b.ts")])
    assert peak == 800_000
    assert average == int((600_000 + 4_000) * 8 / 6.03)


# --- the coordinator and its helpers ---------------------------------------------------


class Store:
    def __init__(self):
        self.objects: dict[str, bytes] = {"uploads/j/s.mov": b"source"}
        self.deleted: list[str] = []

    def download_file(self, *, key, destination):
        Path(destination).write_bytes(self.objects[key])

    def upload_file(self, *, key, source, content_type="video/mp4"):
        self.objects[key] = Path(source).read_bytes()

    def delete_prefix(self, prefix):
        self.deleted.append(prefix)
        for key in [k for k in self.objects if k.startswith(prefix)]:
            del self.objects[key]


class FakeTools:
    """ffprobe answers for a 95 s 30 fps source; ffmpeg writes what each
    command names, two segments per rung per piece."""

    def __init__(self, fail_piece=None):
        self.commands: list[list[str]] = []
        self.fail_piece = fail_piece

    def __call__(self, command, **_kwargs):
        self.commands.append(command)
        if command[0] == "ffprobe":
            return self._probe(command)
        out = Path(command[-1])
        if "-filter_complex" in command:
            index = int(Fraction(command[command.index("-output_ts_offset") + 1]) // 30)
            if index == self.fail_piece:
                return SimpleNamespace(returncode=1, stderr="encode failed")
            root = out.parent.parent
            for k in range(command.count("libx264")):
                rung = root / f"v{k}"
                for n in range(2):
                    (rung / f"seg{n:05d}.ts").write_bytes(b"v" * (1000 * (k + 1)))
                (rung / "index.m3u8").write_text(
                    "#EXTM3U\n#EXTINF:6.000000,\nseg00000.ts\n#EXTINF:6.000000,\nseg00001.ts\n"
                    "#EXT-X-ENDLIST\n"
                )
            return SimpleNamespace(returncode=0, stderr="")
        # The audio.
        (out.parent / "seg00000.ts").write_bytes(b"a" * 500)
        out.write_text("#EXTM3U\n#EXTINF:6.000000,\nseg00000.ts\n#EXT-X-ENDLIST\n")
        return SimpleNamespace(returncode=0, stderr="")

    def _probe(self, command):
        if "packet=pts:stream=time_base:format=start_time" in command:
            packets = [{"pts": k} for k in range(95 * 30)]
            data = {"packets": packets, "streams": [{"time_base": "1/30"}], "format": {}}
            return SimpleNamespace(returncode=0, stdout=json.dumps(data), stderr="")
        if "stream=r_frame_rate,avg_frame_rate" in command:
            data = {"streams": [{"r_frame_rate": "30/1", "avg_frame_rate": "30/1"}]}
            return SimpleNamespace(returncode=0, stdout=json.dumps(data), stderr="")
        height = {0: 360, 1: 720}.get(int(Path(command[-1]).parent.name[1:]), 1080)
        stream = {"width": height * 16 // 9, "height": height, "profile": "High", "level": 31}
        return SimpleNamespace(returncode=0, stdout=json.dumps({"streams": [stream]}), stderr="")

    def pieces(self):
        return [c for c in self.commands if "-filter_complex" in c]


def coordinator(store, redis, tools, **overrides):
    options = dict(enqueue_helper=lambda _job: None, output_prefix="outputs", runner=tools,
                   sleep=lambda _s: None)
    options.update(overrides)
    return LadderPieces(store, redis, **options)


def build(pieces, store, tools):
    builder = HlsLadderBuilder(store, output_prefix="outputs", runner=tools, pieces=pieces)
    probe = SourceProbe(width=1920, height=1080, duration_seconds=95.0, hdr=True, frame_rate=30.0)
    job_id = uuid4()
    key = builder.run(job_id=job_id, source_key="uploads/j/s.mov", probe=probe, max_fps=30)
    return job_id, key


def test_with_no_helper_the_coordinator_encodes_every_piece_and_joins_each_rung():
    store, tools, redis = Store(), FakeTools(), fakeredis.FakeRedis()
    job_id, key = build(coordinator(store, redis, tools), store, tools)

    assert len(tools.pieces()) == 3, "95 s in 30 s pieces, the remainder in the last"
    prefix = f"outputs/{job_id}/hls"
    rung = store.objects[f"{prefix}/v0/index.m3u8"].decode()
    assert [line for line in rung.splitlines() if line.endswith(".ts")] == [
        f"seg{n:05d}.ts" for n in range(6)
    ]
    master = store.objects[key].decode()
    assert "RESOLUTION=640x360" in master and "FRAME-RATE=30.000" in master
    assert 'URI="v3/index.m3u8"' in master
    assert redis.keys("flickpond:pieces:*") == []
    assert not [k for k in store.objects if "ladder-pieces" in k]


def test_a_helper_encodes_pieces_the_coordinator_then_joins():
    store, tools, redis = Store(), FakeTools(), fakeredis.FakeRedis()
    helped = []

    def helper(job_id):
        helped.append(help_ladder(job_id, store=store, connection=redis,
                                  output_prefix="outputs", runner=tools))

    build(coordinator(store, redis, tools, enqueue_helper=helper, helpers=1), store, tools)
    # The helper ran before the coordinator took any: it encoded them all.
    assert helped == [3]


def test_short_videos_and_uncuttable_ones_take_the_single_run(monkeypatch):
    pieces = coordinator(Store(), fakeredis.FakeRedis(), FakeTools(), min_duration_seconds=120)
    assert not pieces.wants(SourceProbe(1920, 1080, 95.0))
    assert pieces.wants(SourceProbe(1920, 1080, 125.0))


def test_a_failed_piece_fails_the_ladder_and_leaves_nothing_behind():
    store, tools, redis = Store(), FakeTools(fail_piece=1), fakeredis.FakeRedis()
    with pytest.raises(Exception, match="ladder piece 1"):
        build(coordinator(store, redis, tools), store, tools)
    assert redis.keys("flickpond:pieces:*") == []
    assert not [k for k in store.objects if "ladder-pieces" in k or "/hls/" in k]


def test_a_helper_that_fails_gives_its_piece_back():
    store, tools, redis = Store(), FakeTools(fail_piece=0), fakeredis.FakeRedis()
    board = board_for(redis, "j")
    board.open(plan(job_id="j"))

    with pytest.raises(Exception, match="ladder piece 0"):
        help_ladder("j", store=store, connection=redis, output_prefix="outputs", runner=tools)
    assert board.take() == 0, "back at the front for someone else"
    assert piece_archive_key("outputs", "j", 0) not in store.objects


def test_a_helper_with_no_plan_does_nothing():
    assert help_ladder("gone", store=Store(), connection=fakeredis.FakeRedis(),
                       output_prefix="outputs", runner=FakeTools()) == 0


def test_the_mp4_and_ladder_boards_of_one_job_never_share_keys():
    from app.worker.chunked import PieceBoard

    redis = fakeredis.FakeRedis()
    PieceBoard(redis, "j").open(SimpleNamespace(to_json=lambda: "{}", count=1))
    assert board_for(redis, "j").plan() is None
