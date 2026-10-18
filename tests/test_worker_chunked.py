"""Chunked MP4: pieces across workers, joined without re-encoding (plan 2.3).

FFmpeg is faked here -- each run just writes its output file -- so these are
about who encodes which piece, what happens when a helper never finishes,
and what the commands say. The joins themselves were checked frame by frame
with real FFmpeg (see app/worker/chunked.py).
"""

from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

import fakeredis
import pytest

from app.queue import HELP_ENCODE_TASK, enqueue_helper, get_chunk_queue
from app.worker import chunked, tasks
from app.worker.chunked import (
    ChunkedMp4,
    PieceBoard,
    PiecePlan,
    frame_starts,
    help_encode,
    piece_command,
    read_frame_times,
)
from app.worker.interruption import Interrupted
from app.worker.probe import SourceProbe
from app.worker.storage import ObjectStoreError, mp4_video_filters

SOURCE = Path("/s")


def frames(duration, fps=30.0):
    return [k / fps for k in range(int(duration * fps))]


def plan(duration=95.0, **overrides) -> PiecePlan:
    fields = dict(
        job_id="j", source_key="uploads/j/src.mov", starts=frame_starts(frames(duration), 10),
        max_fps=30, hdr=False, max_height=1080, preset="veryfast", crf=23,
    )
    fields.update(overrides)
    return PiecePlan(**fields)


class FakeStore:
    def __init__(self):
        self.objects: dict[str, bytes] = {}
        self.deleted_prefixes: list[str] = []
        self.downloads: list[str] = []

    def download_file(self, *, key, destination):
        self.downloads.append(key)
        Path(destination).write_bytes(self.objects.get(key, b"source"))

    def upload_file(self, *, key, source, content_type="video/mp4"):
        self.objects[key] = Path(source).read_bytes()

    def delete_prefix(self, prefix):
        self.deleted_prefixes.append(prefix)
        for key in [k for k in self.objects if k.startswith(prefix)]:
            del self.objects[key]


class FakeFfmpeg:
    """Writes each output; remembers every command. `no_audio` answers the
    audio command as FFmpeg does for a silent video."""

    def __init__(self, *, no_audio=False, audio_error=None, fail_on=None, duration=95.0,
                 frame_times=True):
        self.commands: list[list[str]] = []
        self.no_audio, self.audio_error, self.fail_on = no_audio, audio_error, fail_on
        self.duration, self.frame_times = duration, frame_times

    def __call__(self, command, **_kwargs):
        self.commands.append(command)
        if command[0] == "ffprobe":
            return self._probe(command)
        if "-vn" in command:
            if self.no_audio:
                return SimpleNamespace(
                    returncode=1, stderr="Output file does not contain any stream"
                )
            if self.audio_error:
                return SimpleNamespace(returncode=1, stderr=self.audio_error)
        if self.fail_on and self.fail_on(command):
            return SimpleNamespace(returncode=1, stderr="encode failed")
        Path(command[-1]).write_bytes(b"piece:" + " ".join(command[-6:]).encode())
        return SimpleNamespace(returncode=0, stderr="")

    def _probe(self, command):
        if "packet=pts_time" in command:
            if not self.frame_times:
                return SimpleNamespace(returncode=1, stdout="", stderr="no packets")
            times = "\n".join(f"{t:.6f}" for t in frames(self.duration))
            return SimpleNamespace(returncode=0, stdout=times + "\nN/A\n", stderr="")
        # A piece being measured for the join: 10 s at 30 fps.
        stream = '{"streams": [{"nb_read_packets": "300", "r_frame_rate": "30/1"}]}'
        return SimpleNamespace(returncode=0, stdout=stream, stderr="")

    def pieces(self):
        return [c for c in self.commands if "-an" in c]

    def join(self):
        return next(c for c in self.commands if "concat" in c)


@pytest.fixture
def redis():
    return fakeredis.FakeRedis()


def coordinator(store, redis, runner, **overrides):
    options = dict(
        enqueue_helper=lambda _job_id: None, output_prefix="outputs", runner=runner,
        sleep=lambda _s: None,
    )
    options.update(overrides)
    return ChunkedMp4(store, redis, **options)


def encode(chunker, tmp_path, *, job_id=None, duration=95.0, hdr=False):
    job_id = job_id or uuid4()
    output = tmp_path / "output.mp4"
    assert chunker.encode(
        job_id=job_id, source_key="uploads/j/src.mov", source_path=SOURCE,
        probe=SourceProbe(width=3840, height=2160, duration_seconds=duration, hdr=hdr),
        max_fps=30, output_path=output, work_dir=tmp_path,
    )
    return job_id, output


# --- the plan --------------------------------------------------------------


def test_pieces_start_on_real_frames_and_the_last_takes_the_remainder():
    assert frame_starts(frames(95.0), 10) == [float(s) for s in range(0, 90, 10)]
    assert frame_starts(frames(60.0), 10) == [float(s) for s in range(0, 60, 10)]
    assert frame_starts(frames(9.5), 10) == [0.0]
    assert frame_starts([], 10) == []


def test_at_29_97_fps_each_piece_starts_on_the_first_frame_after_its_mark():
    """The case that put a join a frame out: 30.0 s falls between frames."""
    fps = 30000 / 1001
    starts = frame_starts(frames(64.3, fps), 10)

    assert [round(s, 4) for s in starts] == [0.0, 10.01, 20.02, 30.03, 40.0066, 50.0166]
    assert all(round(s * fps, 6).is_integer() for s in starts)


def test_frame_times_arrive_unsorted_from_b_frames_and_are_sorted():
    assert frame_starts([0.1, 0.0, 10.2, 10.0, 20.5, 20.0, 31.0], 10) == [0.0, 10.0, 20.0]


def test_each_piece_seeks_just_before_its_first_frame_and_stops_short_of_the_next():
    fps = 30000 / 1001
    split = plan(starts=frame_starts(frames(64.3, fps), 10))
    seek, length = split.bounds(3)

    assert seek == pytest.approx(split.starts[3] - 0.001)
    assert seek + length < split.starts[4], "the next piece's first frame is not taken"
    assert seek + length > split.starts[4] - 1 / fps, "this piece's last frame is"
    assert split.bounds(0)[0] == 0.0
    assert split.bounds(split.count - 1)[1] is None


def test_frame_times_are_read_from_packets_and_unreadable_ones_are_skipped():
    def ffprobe(command, **_kwargs):
        assert "packet=pts_time" in command and "-select_streams" in command
        return SimpleNamespace(returncode=0, stdout="0.000000\nN/A\n0.033333,\n", stderr="")

    assert read_frame_times(SOURCE, ffprobe_binary="ffprobe", runner=ffprobe, timeout=5) == [
        0.0,
        0.033333,
    ]


def test_a_plan_survives_the_trip_through_redis():
    assert PiecePlan.from_json(plan(hdr=True).to_json()) == plan(hdr=True)


def test_a_piece_is_the_single_encodes_picture_video_only_from_its_exact_start():
    command = piece_command(
        plan(hdr=True), 3, source_path=SOURCE, output_path=Path("/o.mp4"), ffmpeg_binary="ffmpeg"
    )

    assert command.index("-ss") < command.index("-i")
    assert command[command.index("-ss") + 1] == "29.999000"
    assert command[command.index("-t") + 1] == "10.000500"
    assert "-an" in command
    chain = command[command.index("-vf") + 1]
    assert chain == ",".join([*mp4_video_filters(30, 1080, True), "setpts=PTS-STARTPTS"])
    assert "-color_trc" in command

    last = piece_command(
        plan(), 8, source_path=SOURCE, output_path=Path("/o.mp4"), ffmpeg_binary="ffmpeg"
    )
    assert "-t" not in last


# --- the board ---------------------------------------------------------------


def test_pieces_are_taken_once_each_and_a_given_back_one_goes_first(redis):
    board = PieceBoard(redis, "j")
    board.open(plan(35.0))

    assert [board.take(), board.take()] == [0, 1]
    board.give_back(1)
    assert board.take() == 1
    board.finish(0)
    assert board.done() == {0}


def test_unclaimed_finds_a_lost_piece_and_a_stale_one_but_not_a_busy_one(redis):
    board = PieceBoard(redis, "j")
    board.open(plan(45.0))  # pieces 0..3
    assert [board.take(now=999), board.take(now=1290)] == [0, 1]
    redis.lpop("flickpond:pieces:j:todo")  # piece 2: popped by a helper that died at once

    orphans = board.unclaimed(4, stale_after=300, now=1300)

    # 0 went stale, 1 is still being worked on, 2 was lost, 3 is still queued.
    assert orphans == [0, 2]
    assert board.unclaimed(4, stale_after=300, now=1300) == [0, 2]


def test_closing_the_board_removes_every_key(redis):
    board = PieceBoard(redis, "j")
    board.open(plan())
    board.finish(board.take())

    board.close()

    assert redis.keys("flickpond:pieces:j:*") == []


# --- the coordinator -----------------------------------------------------------


def test_with_no_helper_the_coordinator_encodes_every_piece_and_joins_them(redis, tmp_path):
    store, ffmpeg, asked = FakeStore(), FakeFfmpeg(), []
    chunker = coordinator(store, redis, ffmpeg, enqueue_helper=asked.append)

    job_id, output = encode(chunker, tmp_path)

    assert len(asked) == 3, "helpers are asked for, whether or not any come"
    assert len(ffmpeg.pieces()) == 9
    join = ffmpeg.join()
    assert join[join.index("-f") + 1] == "concat" and join[join.index("-c") + 1] == "copy"
    assert "1:a" in join, "the audio, encoded once, is muxed in"
    listing = (tmp_path / "pieces.txt").read_text().splitlines()
    assert listing[0::2] == [
        f"file '{(tmp_path / f'piece-{i:04d}.mp4').as_posix()}'" for i in range(9)
    ]
    assert set(listing[1::2]) == {"duration 10.000000000"}, "each piece's measured duration"
    assert output.is_file()
    assert redis.keys("flickpond:pieces:*") == []
    assert store.deleted_prefixes == [f"outputs/{job_id}/pieces/"] * 2


def test_pieces_a_helper_finished_are_fetched_and_joined_in_order(redis, tmp_path):
    store, ffmpeg = FakeStore(), FakeFfmpeg()

    def helper_takes_two(job_id):
        board = PieceBoard(redis, job_id)
        for _ in range(2):
            index = board.take()
            store.objects[chunked.piece_key("outputs", job_id, index)] = b"helped"
            board.finish(index)

    chunker = coordinator(store, redis, ffmpeg, enqueue_helper=helper_takes_two, helpers=1)
    job_id, _ = encode(chunker, tmp_path)

    assert len(ffmpeg.pieces()) == 7
    assert (tmp_path / "piece-0000.mp4").read_bytes() == b"helped"
    assert store.downloads == [
        f"outputs/{job_id}/pieces/0000.mp4",
        f"outputs/{job_id}/pieces/0001.mp4",
    ]


def test_a_piece_whose_helper_died_is_encoded_by_the_coordinator(redis, tmp_path, monkeypatch):
    store, ffmpeg = FakeStore(), FakeFfmpeg()
    monkeypatch.setattr(chunked.time, "time", lambda: 10_000.0)

    def helper_dies_holding_one(job_id):
        PieceBoard(redis, job_id).take(now=10_000.0 - 301)

    chunker = coordinator(store, redis, ffmpeg, enqueue_helper=helper_dies_holding_one, helpers=1)
    encode(chunker, tmp_path)

    assert len(ffmpeg.pieces()) == 9


def test_the_coordinator_waits_for_a_helper_still_at_work(redis, tmp_path):
    store, ffmpeg, waits = FakeStore(), FakeFfmpeg(), []
    held = {}

    def helper_holds_one(job_id):
        held["job"] = job_id
        held["index"] = PieceBoard(redis, job_id).take()

    def sleep(_seconds):
        waits.append(1)
        board = PieceBoard(redis, held["job"])
        store.objects[chunked.piece_key("outputs", held["job"], held["index"])] = b"late"
        board.finish(held["index"])

    chunker = coordinator(
        store, redis, ffmpeg, enqueue_helper=helper_holds_one, helpers=1, sleep=sleep
    )
    encode(chunker, tmp_path)

    assert waits == [1]
    assert len(ffmpeg.pieces()) == 8


def test_pieces_not_done_in_time_fail_the_job_readably_and_clean_up(redis, tmp_path):
    store, ffmpeg = FakeStore(), FakeFfmpeg()
    ticks = iter([0, 0, 2000])
    chunker = coordinator(store, redis, ffmpeg, clock=lambda: next(ticks), timeout_seconds=1770)

    with pytest.raises(ObjectStoreError, match="not finished") as caught:
        encode(chunker, tmp_path)

    assert "too long" in caught.value.user_message
    assert redis.keys("flickpond:pieces:*") == []


def test_a_silent_video_is_joined_without_an_audio_track(redis, tmp_path):
    ffmpeg = FakeFfmpeg(no_audio=True)
    encode(coordinator(FakeStore(), redis, ffmpeg), tmp_path)

    assert "1:a" not in ffmpeg.join()


def test_an_audio_failure_is_not_mistaken_for_silence(redis, tmp_path):
    ffmpeg = FakeFfmpeg(audio_error="Invalid data found when processing input")

    with pytest.raises(ObjectStoreError, match="audio"):
        encode(coordinator(FakeStore(), redis, ffmpeg), tmp_path)


def test_a_failed_piece_fails_the_job_and_leaves_no_board(redis, tmp_path):
    ffmpeg = FakeFfmpeg(fail_on=lambda c: "-an" in c and c[c.index("-ss") + 1] == "19.999000")

    with pytest.raises(ObjectStoreError, match="piece 2"):
        encode(coordinator(FakeStore(), redis, ffmpeg), tmp_path)

    assert redis.keys("flickpond:pieces:*") == []


def test_a_source_without_readable_frame_times_is_left_to_one_encode(redis, tmp_path):
    asked, ffmpeg = [], FakeFfmpeg(frame_times=False)
    chunker = coordinator(FakeStore(), redis, ffmpeg, enqueue_helper=asked.append)

    declined = chunker.encode(
        job_id=uuid4(), source_key="k", source_path=SOURCE,
        probe=SourceProbe(1920, 1080, 95.0), max_fps=30, output_path=tmp_path / "o.mp4",
        work_dir=tmp_path,
    )

    assert declined is False
    assert asked == [] and ffmpeg.pieces() == []
    assert redis.keys("flickpond:pieces:*") == []


def test_only_long_sources_are_chunked(redis):
    chunker = coordinator(FakeStore(), redis, FakeFfmpeg(), min_duration_seconds=60)

    assert chunker.wants(SourceProbe(1920, 1080, 60.0))
    assert not chunker.wants(SourceProbe(1920, 1080, 59.9))
    assert not chunker.wants(None)


# --- the helper ------------------------------------------------------------------


def helped(store, redis, ffmpeg):
    return help_encode("j", store=store, connection=redis, output_prefix="outputs", runner=ffmpeg)


def test_a_helper_encodes_until_none_are_left_downloading_the_source_once(redis):
    store, ffmpeg = FakeStore(), FakeFfmpeg()
    PieceBoard(redis, "j").open(plan(35.0))

    assert helped(store, redis, ffmpeg) == 3
    assert store.downloads == ["uploads/j/src.mov"]
    assert sorted(store.objects) == [f"outputs/j/pieces/{i:04d}.mp4" for i in range(3)]
    assert PieceBoard(redis, "j").done() == {0, 1, 2}


def test_a_helper_that_arrives_after_the_job_does_nothing(redis):
    store = FakeStore()

    assert help_encode("gone", store=store, connection=redis, output_prefix="outputs") == 0
    assert store.downloads == []


def test_a_helper_that_fails_gives_its_piece_back(redis):
    PieceBoard(redis, "j").open(plan(35.0))
    ffmpeg = FakeFfmpeg(fail_on=lambda c: True)

    with pytest.raises(ObjectStoreError):
        helped(FakeStore(), redis, ffmpeg)

    assert PieceBoard(redis, "j").take() == 0, "piece 0 is back at the front"


def test_a_helper_does_not_upload_a_piece_for_a_job_that_finished_meanwhile(redis):
    store, board = FakeStore(), PieceBoard(redis, "j")
    board.open(plan(35.0))

    def ffmpeg(command, **_kwargs):
        Path(command[-1]).write_bytes(b"x")
        board.close()
        return SimpleNamespace(returncode=0, stderr="")

    assert helped(store, redis, ffmpeg) == 0
    assert store.objects == {}


# --- wiring ---------------------------------------------------------------------


def test_a_helper_is_queued_on_the_pieces_queue(redis):
    job_id = uuid4()

    enqueue_helper(job_id, queue=get_chunk_queue(redis))

    queued = get_chunk_queue(redis).jobs
    assert [(j.func_name, j.args) for j in queued] == [(HELP_ENCODE_TASK, (str(job_id),))]


def test_a_stopped_helper_ends_quietly_having_given_its_piece_back(monkeypatch):
    def interrupted(*_args, **_kwargs):
        raise Interrupted("not starting ffmpeg: the worker is stopping")

    monkeypatch.setattr(chunked, "help_encode", interrupted)
    monkeypatch.setattr(tasks, "stop_requested", lambda: True)
    monkeypatch.setattr("app.queue.get_redis_connection", lambda: fakeredis.FakeRedis())

    assert tasks.help_encode(str(uuid4())) == "stopped; piece given back"


def test_a_failing_helper_is_reported_when_nobody_asked_it_to_stop(monkeypatch):
    def broken(*_args, **_kwargs):
        raise ObjectStoreError("FFmpeg failed", user_message="x")

    monkeypatch.setattr(chunked, "help_encode", broken)
    monkeypatch.setattr(tasks, "stop_requested", lambda: False)
    monkeypatch.setattr("app.queue.get_redis_connection", lambda: fakeredis.FakeRedis())

    with pytest.raises(ObjectStoreError):
        tasks.help_encode(str(uuid4()))


def test_the_processor_chunks_a_long_source_and_encodes_a_short_one_whole(redis, tmp_path):
    from app.worker.storage import FfmpegProcessor

    calls = []

    class Chunker:
        def wants(self, probe):
            return probe.duration_seconds >= 60

        def encode(self, *, output_path, probe, **_kwargs):
            if probe.duration_seconds > 100:
                return False  # declined: no frame times to cut on
            calls.append("chunked")
            output_path.write_bytes(b"joined")
            return True

    def runner(command, **_kwargs):
        calls.append("whole")
        Path(command[-1]).write_bytes(b"one")
        return SimpleNamespace(returncode=0, stderr="")

    class Store(FakeStore):
        def object_exists(self, key):
            return True

    for duration in (95.0, 20.0, 150.0):
        FfmpegProcessor(
            Store(), output_prefix="outputs", runner=runner, chunked=Chunker(),
            prober=lambda _p, d=duration: SourceProbe(1920, 1080, d),
        ).run(job_id=uuid4(), source_key="uploads/a/b.mp4")

    assert calls == ["chunked", "whole", "whole"]
