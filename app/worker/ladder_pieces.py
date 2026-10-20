"""The HD ladder encoded in pieces across workers (sprint 6, chunked phase 2).

One FFmpeg run makes every rung of the ladder, so a 4K upload's HD versions
take as long as one worker takes: a 4:55 4K 60 fps upload ran past the
whole 30 minutes on flickpond.com (20 Oct 2026). Here the ladder job becomes
a coordinator, as `app.worker.chunked` does for the MP4: it cuts the video
into `piece_seconds` pieces on a Redis board, asks helpers on the pieces
queue to take some, encodes pieces itself, and joins them.

A piece is every rung of its stretch of video in one FFmpeg run -- decoding
and tone-mapping once, as the single run does -- written as MPEG-TS
segments. The join is only playlists: each rung's playlist lists every
piece's segments in order. That works because each piece is placed exactly
where the single run would put it:

* **Frames exactly as one encode.** The same machinery as the chunked MP4
  (`chunked.frame_starts`, `read_frame_ticks`): every frame keeps its own
  timestamp through the rate filter, a piece starts on a frame that begins
  an output frame, and stops after a counted number of frames.
* **Timestamps continuous.** Each piece's output starts at zero and
  `-output_ts_offset` moves it to its place, 10 s on for every piece: piece
  0 at offset 0 alone was pushed a frame later (its first B-frame's decode
  time) and overlapped piece 1. The audio, encoded once, gets the same 10 s,
  or players would line it up 10 s out.
* **A keyframe at every segment**, forced every `SEGMENT_SECONDS` of the
  piece, so every piece and segment starts on one.

Checked on a 120 s 4K HDR 59.94 fps clip, 360/720/1080p: every rung the
single run's 3,600 frames and 120.000 s, no gap or overlap between any two
segments, and the same quality against the source (42.81 dB pieced, 42.73
single, at 360p).

Pieces are uploaded as one tar each, read back with tarfile's `data` filter.
The coordinator never depends on a helper, exactly as for the MP4: with none
it encodes every piece itself.
"""

import json
import logging
import math
import shutil
import tarfile
import tempfile
import time
from collections.abc import Callable
from dataclasses import asdict, dataclass
from fractions import Fraction
from pathlib import Path
from uuid import UUID

from redis import Redis

from app.worker.chunked import (
    PieceBoard,
    _run_ffmpeg,
    output_tick,
    read_frame_rate,
    read_frame_ticks,
)
from app.worker.interruption import run as run_process
from app.worker.storage import BT709_TAGS, UNPROCESSABLE_VIDEO, ObjectStore, ObjectStoreError

logger = logging.getLogger("app.worker.ladder_pieces")

# Every piece's segments sit this far on in the timeline; see the module notes.
TIMELINE_START = 10
AUDIO_BITRATE = 128_000


@dataclass(frozen=True, slots=True)
class LadderPlan:
    """Everything a helper needs to encode a ladder piece as the coordinator would."""

    job_id: str
    source_key: str
    heights: list[int]
    hdr: bool
    max_fps: int
    preset: str
    output_rate: str
    # Per piece: where to seek (seconds), its first frame's own timestamp
    # (stream time base units), where its output starts (output frames),
    # and how many output frames it writes (0: to the end).
    starts: list[float]
    start_ticks: list[int]
    first_frames: list[int]
    frames: list[int]

    @property
    def count(self) -> int:
        return len(self.starts)

    def to_json(self) -> str:
        return json.dumps(asdict(self))

    @classmethod
    def from_json(cls, raw: str | bytes) -> "LadderPlan":
        return cls(**json.loads(raw))


def segment_aligned_starts(times: list, rate: Fraction, piece_seconds: int) -> list:
    """Each piece's first source frame, so that every piece is whole segments.

    Keyframes are forced every `SEGMENT_SECONDS` of a piece, so a segment
    starts at output frame ceil(n * SEGMENT_SECONDS * rate). A piece ending
    anywhere else leaves a sliver segment -- a frame or two, a keyframe --
    whose bitrate made a 360p rung read as 3 Mbps in the master playlist.
    So a piece is exactly as many output frames as its whole segments, and
    starts on the source frame that begins its first output frame (or the
    next one that begins any, in a variable-rate video). A remainder under
    half a piece joins the last.
    """
    from app.worker.hls import SEGMENT_SECONDS

    segments = max(1, round(piece_seconds / SEGMENT_SECONDS))
    length = math.ceil(segments * SEGMENT_SECONDS * rate)
    begins: dict[int, object] = {}
    previous = None
    for time_ in sorted(times):
        tick = output_tick(time_, rate)
        if previous is None or tick > previous:
            begins.setdefault(tick, time_)
        previous = tick
    if not begins:
        return []
    last = max(begins)
    starts, target = [], min(begins)
    while target <= last:
        while target not in begins and target <= last:
            target += 1
        if target > last:
            break
        starts.append(begins[target])
        target += length
    if len(starts) > 1 and last - output_tick(starts[-1], rate) < length / 2:
        starts.pop()
    return starts


def board_for(connection: Redis, job_id: UUID | str) -> PieceBoard:
    return PieceBoard(connection, f"{job_id}:ladder", plan_type=LadderPlan)


def piece_archive_key(output_prefix: str, job_id: UUID | str, index: int) -> str:
    return f"{pieces_prefix(output_prefix, job_id)}{index:04d}.tar"


def pieces_prefix(output_prefix: str, job_id: UUID | str) -> str:
    return f"{output_prefix.strip('/')}/{job_id}/ladder-pieces/"


def piece_command(
    plan: LadderPlan, index: int, *, source_path: Path, out_dir: Path, ffmpeg_binary: str
) -> list[str]:
    """Every rung of one piece, as HLS segments in `out_dir/v{k}/`."""
    from app.worker.hls import (
        BITRATES,
        FALLBACK_BITRATE,
        LADDER_CRF,
        SEGMENT_SECONDS,
        _double,
        _ladder_filter,
    )

    heights = tuple(plan.heights)
    rate = Fraction(plan.output_rate)
    # The single run's graph with its rate cap replaced: each frame back on
    # its own timestamp, the rate as a number (the shift loses the source's),
    # then the piece rebased to zero, which -output_ts_offset undoes below.
    head = (
        f"setpts=PTS-STARTPTS+{plan.start_ticks[index]},"
        f"fps=fps={rate.numerator}/{rate.denominator},setpts=PTS-STARTPTS"
    )
    graph = _ladder_filter(heights, plan.max_fps, plan.hdr, rate_filter=head)
    seek = max(0.0, plan.starts[index] - 0.001)
    command = [
        ffmpeg_binary, "-hide_banner", "-loglevel", "error",
        "-ss", f"{seek:.6f}", "-i", str(source_path),
        "-filter_complex", graph,
    ]
    for k, height in enumerate(heights):
        ceiling = BITRATES.get(height, FALLBACK_BITRATE)
        command += [
            "-map", f"[v{k}out]", f"-c:v:{k}", "libx264", "-preset", plan.preset,
            f"-crf:v:{k}", str(LADDER_CRF),
            f"-maxrate:v:{k}", ceiling, f"-bufsize:v:{k}", _double(ceiling),
        ]
    if plan.frames[index]:
        # By count, not time: a time limit is rounded by the rate filter too.
        command += ["-frames:v", str(plan.frames[index])]
    first = Fraction(plan.first_frames[index]) / rate
    if plan.hdr:
        command += BT709_TAGS
    command += [
        # The rebase loses the rate; HLS output would time frames at 25 fps.
        "-r", f"{rate.numerator}/{rate.denominator}",
        "-force_key_frames", f"expr:gte(t,n_forced*{SEGMENT_SECONDS})",
        "-output_ts_offset", f"{float(first) + TIMELINE_START:.6f}",
        "-f", "hls", "-hls_time", str(SEGMENT_SECONDS), "-hls_playlist_type", "vod",
        "-hls_segment_filename", str(out_dir / "v%v" / "seg%05d.ts"),
        "-var_stream_map", " ".join(f"v:{k}" for k in range(len(heights))),
        "-y", str(out_dir / "v%v" / "index.m3u8"),
    ]
    return command


def audio_command(source_path: Path, out_dir: Path, *, ffmpeg_binary: str) -> list[str]:
    """The whole file's audio as its own rendition, placed like the video."""
    from app.worker.hls import SEGMENT_SECONDS

    return [
        ffmpeg_binary, "-hide_banner", "-loglevel", "error", "-i", str(source_path),
        "-vn", "-map", "0:a:0", "-c:a", "aac", "-b:a", "128k",
        "-output_ts_offset", str(TIMELINE_START),
        "-f", "hls", "-hls_time", str(SEGMENT_SECONDS), "-hls_playlist_type", "vod",
        "-hls_segment_filename", str(out_dir / "seg%05d.ts"),
        "-y", str(out_dir / "index.m3u8"),
    ]


def _encode_piece(plan, index, *, source_path, work_dir, ffmpeg_binary, runner, timeout) -> Path:
    """Encode piece `index` and pack it: one tar of `v{k}/` playlists and segments."""
    out_dir = work_dir / f"piece-{index:04d}"
    shutil.rmtree(out_dir, ignore_errors=True)
    for k in range(len(plan.heights)):
        (out_dir / f"v{k}").mkdir(parents=True)
    _run_ffmpeg(
        runner,
        piece_command(
            plan, index, source_path=source_path, out_dir=out_dir, ffmpeg_binary=ffmpeg_binary
        ),
        timeout=timeout,
        what=f"ladder piece {index}",
    )
    archive = work_dir / f"piece-{index:04d}.tar"
    with tarfile.open(archive, "w") as tar:
        tar.add(out_dir, arcname=".")
    shutil.rmtree(out_dir, ignore_errors=True)
    return archive


def _unpack(archive: Path, destination: Path) -> Path:
    destination.mkdir(parents=True, exist_ok=True)
    with tarfile.open(archive) as tar:
        # `data`: no absolute paths, no links out, no devices.
        tar.extractall(destination, filter="data")
    return destination


def _segments(playlist: Path) -> list[tuple[float, str]]:
    """(duration, file name) of every segment, in order."""
    lines = playlist.read_text(encoding="utf-8").splitlines()
    return [
        (float(line.split(":", 1)[1].rstrip(",")), name)
        for line, name in zip(lines, lines[1:], strict=False)
        if line.startswith("#EXTINF:")
    ]


def _write_playlist(path: Path, entries: list[tuple[float, str]]) -> None:
    target = max((math.ceil(duration) for duration, _ in entries), default=1)
    lines = [
        "#EXTM3U", "#EXT-X-VERSION:3", f"#EXT-X-TARGETDURATION:{target}",
        "#EXT-X-MEDIA-SEQUENCE:0", "#EXT-X-PLAYLIST-TYPE:VOD",
    ]
    for duration, name in entries:
        lines += [f"#EXTINF:{duration:.6f},", name]
    lines.append("#EXT-X-ENDLIST")
    path.write_text("\n".join(lines) + "\n", encoding="utf-8", newline="\n")


def _rates(directory: Path, entries: list[tuple[float, str]]) -> tuple[int, int]:
    """(peak, average) bits per second over the segments.

    The peak leaves out a final sliver under a second -- the end of the
    file, wherever it falls -- whose few packets over a tiny duration read
    as a rate nothing sustains (the audio's 128 kbps measured 389).
    """
    sizes = [(directory / name).stat().st_size * 8 for _, name in entries]
    seconds = [duration for duration, _ in entries]
    pairs = list(zip(sizes, seconds, strict=True))
    whole = [bits / s for bits, s in pairs if s >= 1]
    peak = max(whole or [bits / s for bits, s in pairs if s > 0] or [0])
    return int(peak), int(sum(sizes) / (sum(seconds) or 1))


# avc1.PPCCLL: profile_idc, constraint flags, level_idc -- as FFmpeg writes them.
_PROFILE_IDC = {"Baseline": 66, "Constrained Baseline": 66, "Main": 77, "High": 100}


def _video_facts(segment: Path, *, ffprobe_binary: str, runner, timeout: int) -> tuple[str, str]:
    """(RESOLUTION, avc1 codec string) of a rung, from one of its segments."""
    command = [
        ffprobe_binary, "-v", "error", "-select_streams", "v:0",
        "-show_entries", "stream=width,height,profile,level", "-of", "json", str(segment),
    ]
    result = runner(command, capture_output=True, text=True, timeout=timeout, check=False)
    try:
        stream = json.loads(result.stdout)["streams"][0]
        profile = _PROFILE_IDC[stream["profile"]]
        flags = 0x40 if stream["profile"] == "Constrained Baseline" else 0
        codec = f"avc1.{profile:02x}{flags:02x}{int(stream['level']):02x}"
        return f"{stream['width']}x{stream['height']}", codec
    except (KeyError, IndexError, ValueError, TypeError) as exc:
        raise ObjectStoreError(
            f"could not read rung facts from {segment.name}: {result.stderr}",
            user_message=UNPROCESSABLE_VIDEO,
        ) from exc


def write_master(
    path: Path, rungs: list[tuple[str, str, int, int]], audio: tuple[int, int], audio_uri: str
) -> None:
    """The master playlist in FFmpeg's own layout, so everything that reads
    it -- `label_frame_rates`, the HDR/AV1 renditions -- reads it unchanged.
    `rungs`: (RESOLUTION, avc1 codec, peak, average) per rung, shortest first."""
    group = 'GROUP-ID="group_audio"'
    lines = [
        "#EXTM3U", "#EXT-X-VERSION:3",
        f'#EXT-X-MEDIA:TYPE=AUDIO,{group},NAME="audio_{len(rungs)}",DEFAULT=YES,'
        f'CHANNELS="2",URI="{audio_uri}"',
    ]
    for k, (resolution, codec, peak, average) in enumerate(rungs):
        lines += [
            f"#EXT-X-STREAM-INF:BANDWIDTH={peak + audio[0]},"
            f"AVERAGE-BANDWIDTH={average + audio[1]},RESOLUTION={resolution},"
            f'CODECS="{codec},mp4a.40.2",AUDIO="group_audio"',
            f"v{k}/index.m3u8",
            "",
        ]
    lines += [
        f"#EXT-X-STREAM-INF:BANDWIDTH={audio[0]},AVERAGE-BANDWIDTH={audio[1]},"
        f'CODECS="mp4a.40.2",AUDIO="group_audio"',
        audio_uri,
    ]
    path.write_text("\n".join(lines) + "\n", encoding="utf-8", newline="\n")


class LadderPieces:
    """The coordinator's side. Used by `HlsLadderBuilder` for long sources."""

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
        piece_seconds: int = 30,
        min_duration_seconds: int = 60,
        helpers: int = 3,
        stale_after_seconds: int = 600,
        timeout_seconds: int = 3570,
        runner=run_process,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
        wake: Callable[[int], object] | None = None,
    ) -> None:
        self._store = store
        self._redis = connection
        self._enqueue_helper = enqueue_helper
        self._output_prefix = output_prefix
        self._ffmpeg = ffmpeg_binary
        self._ffprobe = ffprobe_binary
        self._preset = preset
        self._piece_seconds = piece_seconds
        self._min_duration = min_duration_seconds
        self._helpers = helpers
        self._stale_after = stale_after_seconds
        self._timeout = timeout_seconds
        self._runner = runner
        self._clock = clock
        self._sleep = sleep
        self._wake = wake

    def wants(self, probe) -> bool:
        return probe is not None and probe.duration_seconds >= self._min_duration

    def plan(self, *, job_id, source_key, source_path, probe, max_fps, heights):
        """The pieces, or None if this source cannot be cut exactly."""
        ticks, time_base = read_frame_ticks(
            source_path, ffprobe_binary=self._ffprobe, runner=self._runner, timeout=self._timeout
        )
        source_rate = read_frame_rate(
            source_path, ffprobe_binary=self._ffprobe, runner=self._runner, timeout=self._timeout
        )
        if not ticks or source_rate is None:
            return None
        rate = min(Fraction(max_fps), source_rate)
        starts = segment_aligned_starts(
            [tick * time_base for tick in ticks], rate, self._piece_seconds
        )
        if len(starts) < 2:
            return None
        first = [output_tick(start, rate) for start in starts]
        return LadderPlan(
            job_id=str(job_id),
            source_key=source_key,
            heights=list(heights),
            hdr=probe.hdr,
            max_fps=max_fps,
            preset=self._preset,
            output_rate=f"{rate.numerator}/{rate.denominator}",
            starts=[float(start) for start in starts],
            start_ticks=[int(start / time_base) for start in starts],
            first_frames=first,
            frames=[b - a for a, b in zip(first, first[1:], strict=False)] + [0],
        )

    def encode(
        self, *, job_id: UUID, source_key: str, source_path: Path, probe, max_fps: int,
        heights: tuple[int, ...], work_dir: Path,
    ) -> bool:
        """Fill `work_dir` as the single run would: `v{k}/` per rung, the
        audio in `v{len(heights)}/`, and `master.m3u8`. False if this source
        cannot be cut exactly -- the caller then makes the ladder in one run."""
        plan = self.plan(
            job_id=job_id, source_key=source_key, source_path=source_path, probe=probe,
            max_fps=max_fps, heights=heights,
        )
        if plan is None:
            logger.info("job %s: ladder cannot be cut exactly; one run instead", job_id)
            return False
        board = board_for(self._redis, job_id)
        prefix = pieces_prefix(self._output_prefix, job_id)
        self._store.delete_prefix(prefix)
        board.open(plan)
        started = self._clock()
        try:
            helpers = min(self._helpers, plan.count - 1)
            for _ in range(helpers):
                self._enqueue_helper(job_id)
            if self._wake is not None and helpers:
                self._wake(1 + helpers)
            logger.info(
                "job %s: ladder %s in %d pieces of about %ss",
                job_id, list(heights), plan.count, self._piece_seconds,
            )
            audio_dir = work_dir / f"v{len(heights)}"
            audio_dir.mkdir(parents=True, exist_ok=True)
            _run_ffmpeg(
                self._runner, audio_command(source_path, audio_dir, ffmpeg_binary=self._ffmpeg),
                timeout=self._timeout, what="the ladder's audio",
            )
            mine = self._encode_until_all_done(plan, board, source_path, work_dir, started)
            pieces_dir = work_dir / "pieces"
            for index in range(plan.count):
                archive = mine.get(index) or self._fetch(job_id, index, work_dir)
                _unpack(archive, pieces_dir / f"{index:04d}")
            self._join(plan, pieces_dir, work_dir, audio_dir)
            logger.info(
                "job %s: ladder joined, %d of %d pieces here", job_id, len(mine), plan.count
            )
            return True
        finally:
            board.close()
            try:
                self._store.delete_prefix(prefix)
            except Exception:
                logger.warning("job %s: ladder pieces left under %s", job_id, prefix)

    def _encode_until_all_done(self, plan, board, source_path, work_dir, started):
        mine: dict[int, Path] = {}
        while True:
            if self._clock() - started > self._timeout:
                raise ObjectStoreError(
                    f"ladder pieces not finished after {self._timeout} seconds",
                    user_message=UNPROCESSABLE_VIDEO,
                )
            index = board.take()
            if index is None:
                orphans = board.unclaimed(plan.count, stale_after=self._stale_after)
                if orphans:
                    index = orphans[0]
                elif len(board.done()) >= plan.count:
                    return mine
                else:
                    self._sleep(1)
                    continue
            mine[index] = _encode_piece(
                plan, index, source_path=source_path, work_dir=work_dir,
                ffmpeg_binary=self._ffmpeg, runner=self._runner, timeout=self._timeout,
            )
            board.finish(index)

    def _fetch(self, job_id: UUID, index: int, work_dir: Path) -> Path:
        path = work_dir / f"piece-{index:04d}.tar"
        self._store.download_file(
            key=piece_archive_key(self._output_prefix, job_id, index), destination=str(path)
        )
        return path

    def _join(self, plan: LadderPlan, pieces_dir: Path, work_dir: Path, audio_dir: Path) -> None:
        rungs = []
        for k in range(len(plan.heights)):
            rung_dir = work_dir / f"v{k}"
            rung_dir.mkdir(parents=True, exist_ok=True)
            entries: list[tuple[float, str]] = []
            for index in range(plan.count):
                piece = pieces_dir / f"{index:04d}" / f"v{k}"
                for duration, name in _segments(piece / "index.m3u8"):
                    joined = f"seg{len(entries):05d}.ts"
                    shutil.move(str(piece / name), str(rung_dir / joined))
                    entries.append((duration, joined))
            _write_playlist(rung_dir / "index.m3u8", entries)
            resolution, codec = _video_facts(
                rung_dir / entries[0][1], ffprobe_binary=self._ffprobe,
                runner=self._runner, timeout=self._timeout,
            )
            rungs.append((resolution, codec, *_rates(rung_dir, entries)))
        audio = _segments(audio_dir / "index.m3u8")
        from app.worker.hls import MASTER_PLAYLIST_NAME

        write_master(
            work_dir / MASTER_PLAYLIST_NAME, rungs, _rates(audio_dir, audio),
            f"v{len(plan.heights)}/index.m3u8",
        )
        shutil.rmtree(pieces_dir, ignore_errors=True)


def help_ladder(
    job_id: UUID | str,
    *,
    store: ObjectStore,
    connection: Redis,
    output_prefix: str,
    ffmpeg_binary: str = "ffmpeg",
    timeout_seconds: int = 3570,
    runner=run_process,
) -> int:
    """The helper's side: encode ladder pieces of `job_id` until none are left.

    As `chunked.help_encode`: the source only once it has a piece, and a
    piece it cannot finish goes back for someone else.
    """
    board = board_for(connection, job_id)
    plan = board.plan()
    if plan is None:
        return 0
    work_dir = Path(tempfile.mkdtemp(prefix="flickpond-ladder-piece-"))
    source_path = work_dir / "source"
    encoded = 0
    try:
        while (index := board.take()) is not None:
            try:
                if not source_path.exists():
                    store.download_file(key=plan.source_key, destination=str(source_path))
                archive = _encode_piece(
                    plan, index, source_path=source_path, work_dir=work_dir,
                    ffmpeg_binary=ffmpeg_binary, runner=runner, timeout=timeout_seconds,
                )
                if board.plan() is None:
                    return encoded
                store.upload_file(
                    key=piece_archive_key(output_prefix, job_id, index),
                    source=str(archive),
                    content_type="application/x-tar",
                )
                board.finish(index)
                encoded += 1
                archive.unlink(missing_ok=True)
            except BaseException:
                board.give_back(index)
                raise
        return encoded
    finally:
        shutil.rmtree(work_dir, ignore_errors=True)
