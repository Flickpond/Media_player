"""A long video's MP4, encoded in pieces across workers (sprint 6 plan, 2.3).

One FFmpeg run on one worker is the floor on how fast a long 4K upload can be
ready, however many workers the pool has. Here the job's own worker becomes a
coordinator: it splits the video into pieces of `chunk_seconds`, puts them on
a list in Redis, queues a few helper jobs, and encodes pieces itself. Helpers
on other workers take pieces from the same list and upload what they encode.
The coordinator joins every piece without re-encoding and adds the audio,
which it encodes once for the whole file -- separately encoded AAC pieces
click at every join.

**The coordinator never depends on a helper.** With no helper at all it
encodes every piece itself. A helper stopped by a spot notice gives its piece
back; one that dies without warning is found by its piece going stale, and
the coordinator encodes that piece itself. The job keeps its own RQ entry,
heartbeat and give-back exactly as before, so the reaper needs no new rule.

Why the pieces join cleanly -- checked frame by frame against a single
encode, at 30 fps and at 29.97 fps with a remainder piece, each giving the
same frame count and duration to the microsecond:

* **Pieces start on real source frames.** The coordinator reads the source's
  frame times (packets only, no decoding) and starts each piece at the first
  frame at or after each mark. A mark between two frames -- every boundary
  of a 29.97 fps video -- otherwise has the rate filter round differently at
  that edge than one encode would, and a frame is added or lost there.
* **Each piece is its own MP4 starting at zero**, and the join tells the
  concat demuxer every piece's measured duration, so each is placed exactly
  after the one before. (MPEG-TS pieces with offset timestamps overlap at
  every join instead: B-frames give a piece decode times before its start.)
"""

import json
import logging
import shutil
import subprocess
import tempfile
import time
from collections.abc import Callable
from dataclasses import asdict, dataclass
from fractions import Fraction
from pathlib import Path
from uuid import UUID

from redis import Redis

from app.worker.interruption import run as run_process
from app.worker.storage import (
    BT709_TAGS,
    UNPROCESSABLE_VIDEO,
    ObjectStore,
    ObjectStoreError,
    mp4_video_filters,
)

logger = logging.getLogger("app.worker.chunked")

# Long enough to outlive any job (RQ's own limit is 30 minutes); every key is
# deleted when the job finishes, so this only bounds what a crash leaves.
BOARD_TTL_SECONDS = 3 * 3600


@dataclass(frozen=True, slots=True)
class PiecePlan:
    """Everything a helper needs to encode a piece exactly as the
    coordinator would. Settings travel with the plan, not with each worker's
    environment, so every piece of one video is encoded the same way."""

    job_id: str
    source_key: str
    # Where each piece starts, in source seconds: real frame times.
    starts: list[float]
    max_fps: int
    hdr: bool
    max_height: int
    preset: str
    crf: int

    @property
    def count(self) -> int:
        return len(self.starts)

    def bounds(self, index: int) -> tuple[float, float | None]:
        """Where to seek and for how long; the last piece runs to the end.

        A millisecond early, so the piece's first frame -- exactly at its
        start -- is never lost to rounding. The length stops half a
        millisecond short of the next start, so that piece's first frame is
        never taken twice; the last frame of this one, a whole frame earlier,
        is well inside.
        """
        start = self.starts[index]
        seek = max(0.0, start - 0.001)
        if index == self.count - 1:
            return seek, None
        return seek, self.starts[index + 1] - seek - 0.0005

    def to_json(self) -> str:
        return json.dumps(asdict(self))

    @classmethod
    def from_json(cls, raw: str | bytes) -> "PiecePlan":
        return cls(**json.loads(raw))


def frame_starts(frame_times: list[float], chunk_seconds: int) -> list[float]:
    """Each piece's start: the first frame at or after every `chunk_seconds`.

    A remainder under half a piece joins the last piece rather than being
    a sliver of its own; anything longer is a piece.
    """
    times = sorted(frame_times)
    starts: list[float] = []
    mark, index = 0.0, 0
    while times and mark <= times[-1]:
        while index < len(times) and times[index] < mark - 1e-6:
            index += 1
        if index == len(times):
            break
        if not starts or times[index] > starts[-1]:
            starts.append(times[index])
        mark += chunk_seconds
    if len(starts) > 1 and times[-1] - starts[-1] < chunk_seconds / 2:
        starts.pop()
    return starts


def read_frame_times(
    source_path: Path, *, ffprobe_binary: str, runner, timeout: int
) -> list[float]:
    """Every video frame's presentation time, from the packets alone."""
    command = [
        ffprobe_binary, "-v", "error", "-select_streams", "v:0",
        "-show_entries", "packet=pts_time", "-of", "csv=p=0", str(source_path),
    ]
    result = runner(command, capture_output=True, text=True, timeout=timeout, check=False)
    if result.returncode != 0:
        return []
    times = []
    for line in (result.stdout or "").splitlines():
        value = line.strip().rstrip(",")
        if value and value != "N/A":
            times.append(float(value))
    return times


class PieceBoard:
    """The pieces of one job in Redis: to do, in progress (with when), done."""

    def __init__(self, connection: Redis, job_id: UUID | str) -> None:
        self._redis = connection
        prefix = f"flickpond:pieces:{job_id}"
        self._plan, self._todo = f"{prefix}:plan", f"{prefix}:todo"
        self._inflight, self._done = f"{prefix}:inflight", f"{prefix}:done"

    def open(self, plan: PiecePlan) -> None:
        keys = (self._plan, self._todo, self._inflight, self._done)
        with self._redis.pipeline() as pipe:
            pipe.delete(*keys)
            pipe.set(self._plan, plan.to_json(), ex=BOARD_TTL_SECONDS)
            pipe.rpush(self._todo, *range(plan.count))
            pipe.expire(self._todo, BOARD_TTL_SECONDS)
            pipe.execute()

    def plan(self) -> PiecePlan | None:
        raw = self._redis.get(self._plan)
        return PiecePlan.from_json(raw) if raw is not None else None

    def take(self, *, now: float | None = None) -> int | None:
        raw = self._redis.lpop(self._todo)
        if raw is None:
            return None
        index = int(raw)
        with self._redis.pipeline() as pipe:
            pipe.hset(self._inflight, index, time.time() if now is None else now)
            pipe.expire(self._inflight, BOARD_TTL_SECONDS)
            pipe.execute()
        return index

    def finish(self, index: int) -> None:
        with self._redis.pipeline() as pipe:
            pipe.sadd(self._done, index)
            pipe.expire(self._done, BOARD_TTL_SECONDS)
            pipe.hdel(self._inflight, index)
            pipe.execute()

    def give_back(self, index: int) -> None:
        """A helper that is stopping returns its piece to the front."""
        with self._redis.pipeline() as pipe:
            pipe.hdel(self._inflight, index)
            pipe.lpush(self._todo, index)
            pipe.execute()

    def done(self) -> set[int]:
        return {int(raw) for raw in self._redis.smembers(self._done)}

    def unclaimed(self, count: int, *, stale_after: float, now: float | None = None) -> list[int]:
        """Pieces nobody will finish: never taken by anyone alive, or taken
        `stale_after` seconds ago and not done -- a helper that died. They
        are the coordinator's to encode; a stale one is no longer in flight.

        Lost between a helper's pop and its in-flight note is covered too:
        it is in none of the lists, so it counts as never taken.
        """
        now = time.time() if now is None else now
        inflight = {int(k): float(v) for k, v in self._redis.hgetall(self._inflight).items()}
        queued = {int(raw) for raw in self._redis.lrange(self._todo, 0, -1)}
        stale = [index for index, since in inflight.items() if now - since > stale_after]
        if stale:
            self._redis.hdel(self._inflight, *stale)
        busy = set(inflight) - set(stale)
        return sorted(set(range(count)) - self.done() - busy - queued)

    def close(self) -> None:
        self._redis.delete(self._plan, self._todo, self._inflight, self._done)


def piece_key(output_prefix: str, job_id: UUID | str, index: int) -> str:
    return f"{pieces_prefix(output_prefix, job_id)}{index:04d}.mp4"


def pieces_prefix(output_prefix: str, job_id: UUID | str) -> str:
    return f"{output_prefix.strip('/')}/{job_id}/pieces/"


def piece_command(
    plan: PiecePlan, index: int, *, source_path: Path, output_path: Path, ffmpeg_binary: str
) -> list[str]:
    """One piece: video only, its own MP4 starting at zero.

    `-ss` before `-i` seeks fast and, with a re-encode, exactly; `-t` after
    it ends the piece where the next begins. The filters are the single
    encode's, so a joined video looks like one encode.
    """
    start, length = plan.bounds(index)
    command = [
        ffmpeg_binary, "-hide_banner", "-loglevel", "error",
        "-ss", f"{start:.6f}", "-i", str(source_path),
    ]
    if length is not None:
        command.extend(["-t", f"{length:.6f}"])
    command.extend([
        "-an",
        # The piece's first frame at exactly zero. A boundary between two
        # frames (any 29.97 fps video) otherwise leaves it a fraction of a
        # frame in, and the concat demuxer, rebasing onto the file's start,
        # puts it one frame out from that join on. Last, not first: before
        # the rate cap it would hide the source's rate from `source_fps`.
        # With starts on real frames this is belt and braces.
        "-vf", ",".join(
            [*mp4_video_filters(plan.max_fps, plan.max_height, plan.hdr), "setpts=PTS-STARTPTS"]
        ),
        *(BT709_TAGS if plan.hdr else []),
        "-c:v", "libx264", "-preset", plan.preset, "-crf", str(plan.crf),
        "-y", str(output_path),
    ])
    return command


def _run_ffmpeg(runner, command: list[str], *, timeout: int, what: str) -> None:
    try:
        result = runner(command, capture_output=True, text=True, timeout=timeout, check=False)
    except subprocess.TimeoutExpired as exc:
        raise ObjectStoreError(
            f"{what} timed out after {timeout} seconds",
            user_message="processing took too long; try a shorter or smaller video",
        ) from exc
    if result.returncode != 0:
        detail = "\n".join((result.stderr or "").strip().splitlines()[-5:])[:1000]
        raise ObjectStoreError(
            f"FFmpeg failed on {what}: {detail or 'no FFmpeg error output'}",
            user_message=UNPROCESSABLE_VIDEO,
        )


class ChunkedMp4:
    """The coordinator's side. Used by `FfmpegProcessor` for long sources."""

    def __init__(
        self,
        store: ObjectStore,
        connection: Redis,
        *,
        enqueue_helper: Callable[[UUID], object],
        output_prefix: str,
        ffmpeg_binary: str = "ffmpeg",
        ffprobe_binary: str = "ffprobe",
        preset: str = "veryfast",
        crf: int = 23,
        max_height: int = 1080,
        chunk_seconds: int = 10,
        min_duration_seconds: int = 60,
        helpers: int = 3,
        stale_after_seconds: int = 300,
        timeout_seconds: int = 1770,
        runner=run_process,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
        progress: Callable[[UUID, float], None] | None = None,
    ) -> None:
        self._store = store
        self._redis = connection
        self._enqueue_helper = enqueue_helper
        self._output_prefix = output_prefix.strip("/")
        self._ffmpeg = ffmpeg_binary
        self._ffprobe = ffprobe_binary
        self._preset, self._crf, self._max_height = preset, crf, max_height
        self._chunk_seconds = chunk_seconds
        self._min_duration = min_duration_seconds
        self._helpers = helpers
        self._stale_after = stale_after_seconds
        self._timeout = timeout_seconds
        self._runner = runner
        self._clock, self._sleep = clock, sleep
        self._progress = progress

    def wants(self, probe) -> bool:
        """Long enough that pieces pay for their overhead."""
        return probe is not None and probe.duration_seconds >= self._min_duration

    def encode(
        self,
        *,
        job_id: UUID,
        source_key: str,
        source_path: Path,
        probe,
        max_fps: int,
        output_path: Path,
        work_dir: Path,
    ) -> bool:
        """Encode `output_path` in pieces. False if this source cannot be cut
        on its own frames -- the caller then encodes it in one run, which is
        always correct, instead of risking a join a frame out."""
        times = read_frame_times(
            source_path, ffprobe_binary=self._ffprobe, runner=self._runner, timeout=self._timeout
        )
        starts = frame_starts(times, self._chunk_seconds)
        if len(starts) < 2:
            logger.info("job %s: no frame times to cut on; one encode instead", job_id)
            return False
        plan = PiecePlan(
            job_id=str(job_id),
            source_key=source_key,
            starts=starts,
            max_fps=max_fps,
            hdr=probe.hdr,
            max_height=self._max_height,
            preset=self._preset,
            crf=self._crf,
        )
        board = PieceBoard(self._redis, job_id)
        prefix = pieces_prefix(self._output_prefix, job_id)
        # Pieces from an attempt that was given back are not trusted: the
        # settings may have changed since.
        self._store.delete_prefix(prefix)
        board.open(plan)
        started = self._clock()
        try:
            for _ in range(min(self._helpers, plan.count - 1)):
                self._enqueue_helper(job_id)
            logger.info("job %s: %d pieces of about %ss", job_id, plan.count, self._chunk_seconds)
            audio = self._encode_audio(source_path, work_dir)
            mine = self._encode_until_all_done(plan, board, source_path, work_dir, started)
            pieces = [
                mine.get(index) or self._fetch(job_id, index, work_dir)
                for index in range(plan.count)
            ]
            logger.info(
                "job %s: joining %d pieces, %d encoded here",
                job_id, plan.count, len(mine),
            )
            self._join(pieces, audio, output_path, work_dir)
            return True
        finally:
            board.close()
            try:
                self._store.delete_prefix(prefix)
            except Exception:
                logger.warning("job %s: pieces left in storage under %s", job_id, prefix)

    def _report(self, plan: PiecePlan, done: int) -> None:
        # Pieces are most of the work; the join and the upload are the rest.
        if self._progress is not None:
            self._progress(UUID(plan.job_id), 90 * done / plan.count)

    def _encode_until_all_done(
        self, plan, board, source_path, work_dir, started
    ) -> dict[int, Path]:
        mine: dict[int, Path] = {}
        while True:
            if self._clock() - started > self._timeout:
                raise ObjectStoreError(
                    f"pieces not finished after {self._timeout} seconds",
                    user_message="processing took too long; try a shorter or smaller video",
                )
            index = board.take()
            if index is None:
                orphans = board.unclaimed(plan.count, stale_after=self._stale_after)
                if orphans:
                    index = orphans[0]
                elif (done := len(board.done())) >= plan.count:
                    return mine
                else:
                    # Helpers are still on their pieces.
                    self._report(plan, done)
                    self._sleep(1)
                    continue
            path = work_dir / f"piece-{index:04d}.mp4"
            _run_ffmpeg(
                self._runner,
                piece_command(
                    plan, index, source_path=source_path, output_path=path,
                    ffmpeg_binary=self._ffmpeg,
                ),
                timeout=self._timeout,
                what=f"piece {index}",
            )
            mine[index] = path
            board.finish(index)
            self._report(plan, len(board.done()))

    def _encode_audio(self, source_path: Path, work_dir: Path) -> Path | None:
        """The whole file's audio, once; None for a video without sound."""
        audio = work_dir / "audio.m4a"
        command = [
            self._ffmpeg, "-hide_banner", "-loglevel", "error", "-i", str(source_path),
            "-vn", "-map", "0:a:0?", "-c:a", "aac", "-y", str(audio),
        ]
        result = self._runner(
            command, capture_output=True, text=True, timeout=self._timeout, check=False
        )
        if result.returncode == 0 and audio.is_file():
            return audio
        if "does not contain any stream" in (result.stderr or ""):
            return None
        detail = (result.stderr or "").strip()[-500:]
        raise ObjectStoreError(
            f"FFmpeg failed on the audio: {detail}", user_message=UNPROCESSABLE_VIDEO
        )

    def _fetch(self, job_id: UUID, index: int, work_dir: Path) -> Path:
        path = work_dir / f"piece-{index:04d}.mp4"
        self._store.download_file(
            key=piece_key(self._output_prefix, job_id, index), destination=str(path)
        )
        return path

    def _piece_duration(self, piece: Path) -> Fraction:
        """Frames over rate, measured. The MP4's own duration can leave out
        the last frame's, and the demuxer would then overlap the next piece."""
        command = [
            self._ffprobe, "-v", "error", "-select_streams", "v:0", "-count_packets",
            "-show_entries", "stream=nb_read_packets,r_frame_rate", "-of", "json", str(piece),
        ]
        result = self._runner(
            command, capture_output=True, text=True, timeout=self._timeout, check=False
        )
        try:
            stream = json.loads(result.stdout)["streams"][0]
            return int(stream["nb_read_packets"]) / Fraction(stream["r_frame_rate"])
        except (KeyError, IndexError, ValueError, ZeroDivisionError, TypeError) as exc:
            raise ObjectStoreError(
                f"could not measure piece {piece.name}: {result.stderr}",
                user_message=UNPROCESSABLE_VIDEO,
            ) from exc

    def _join(self, pieces: list[Path], audio: Path | None, output_path: Path, work_dir: Path):
        listing = work_dir / "pieces.txt"
        entries = [
            f"file '{piece.as_posix()}'\nduration {float(self._piece_duration(piece)):.9f}\n"
            for piece in pieces
        ]
        listing.write_text("".join(entries), encoding="utf-8", newline="\n")
        command = [
            self._ffmpeg, "-hide_banner", "-loglevel", "error",
            "-f", "concat", "-safe", "0", "-i", str(listing),
        ]
        if audio is not None:
            command.extend(["-i", str(audio), "-map", "0:v", "-map", "1:a"])
        command.extend(["-c", "copy", "-movflags", "+faststart", "-y", str(output_path)])
        _run_ffmpeg(self._runner, command, timeout=self._timeout, what="the join")


def help_encode(
    job_id: UUID | str,
    *,
    store: ObjectStore,
    connection: Redis,
    output_prefix: str,
    ffmpeg_binary: str = "ffmpeg",
    timeout_seconds: int = 1770,
    runner=run_process,
) -> int:
    """The helper's side: encode pieces of `job_id` until none are left.

    Returns how many it encoded. Downloads the source only once it has a
    piece, so a helper that arrives late costs nothing. A piece it cannot
    finish -- a stop, a failure -- goes back for someone else.
    """
    board = PieceBoard(connection, job_id)
    plan = board.plan()
    if plan is None:
        return 0
    work_dir = Path(tempfile.mkdtemp(prefix="flickpond-piece-"))
    source_path = work_dir / "source"
    encoded = 0
    try:
        while (index := board.take()) is not None:
            try:
                if not source_path.exists():
                    store.download_file(key=plan.source_key, destination=str(source_path))
                path = work_dir / f"piece-{index:04d}.mp4"
                _run_ffmpeg(
                    runner,
                    piece_command(
                        plan, index, source_path=source_path, output_path=path,
                        ffmpeg_binary=ffmpeg_binary,
                    ),
                    timeout=timeout_seconds,
                    what=f"piece {index}",
                )
                if board.plan() is None:
                    # The job finished, failed or was given back meanwhile.
                    return encoded
                store.upload_file(key=piece_key(output_prefix, job_id, index), source=str(path))
                board.finish(index)
                encoded += 1
                path.unlink(missing_ok=True)
            except BaseException:
                board.give_back(index)
                raise
        return encoded
    finally:
        shutil.rmtree(work_dir, ignore_errors=True)
