"""Extra AV1 renditions added to a finished ladder: Keep HDR, and popular videos.

The ladder every video gets is H.264, standard range: it plays everywhere.
Two kinds of video earn one more rendition on top of it, encoded in AV1 at up
to 1080p and listed in the same master playlist:

* **hdr** -- an HDR upload, kept HDR (10-bit, PQ or HLG as recorded) when
  `WORKER_KEEP_HDR` is on. The playlist marks it `VIDEO-RANGE=PQ`/`HLG`, and
  hls.js (1.7) offers it only on a screen that reports `dynamic-range: high`;
  every other screen keeps playing the tone-mapped ladder.
* **av1** -- a video enough people have watched (`AV1_POPULAR_VIEWS`): the
  same picture as the ladder for fewer bytes, where the browser decodes AV1.

Each is its own job on the ladder queue, after the ladder is ready, so it can
neither delay nor break the video: if it fails, the video simply has no extra
rendition. The master playlist is uploaded last, so a player never sees a
rendition whose segments are not there yet.

**Order in the master is what makes a player use them** (checked in Chromium
with hls.js 1.7): hls.js starts in the codec family of the first entry it can
play and stays there. So the entries go first -- HDR, then AV1, then the
ladder. An SDR screen skips the HDR entries by itself and starts in AV1; a
browser without AV1 skips both and plays the ladder as before. AV1 gets two
sizes so a slow connection can step down without leaving it.
"""

import logging
import re
import shutil
import tempfile
from dataclasses import dataclass
from pathlib import Path

from app.worker.interruption import run as run_process
from app.worker.probe import SourceProbe
from app.worker.storage import (
    BT709_TAGS,
    UNPROCESSABLE_VIDEO,
    ObjectStore,
    ObjectStoreError,
    fps_cap_filter,
    sdr_filters,
)

logger = logging.getLogger("app.worker.renditions")

KINDS = ("hdr", "av1")
# Heights per kind, largest first; a source shorter than one keeps its own.
RUNGS = {"hdr": (1080,), "av1": (1080, 540)}
SEGMENT_SECONDS = 4
# Position in the master, ahead of the ladder (see the module docstring).
ORDER = {"hdr": 0, "av1": 1}

# The AV1 codec strings a player checks before choosing a rendition: main
# profile, level 4.0 (1080p), then bit depth -- and for HDR the colour: 4:2:0,
# BT.2020 primaries, PQ (16) or HLG (18), BT.2020 matrix, limited range.
TRANSFER_CODE = {"smpte2084": "16", "arib-std-b67": "18"}
VIDEO_RANGE = {"smpte2084": "PQ", "arib-std-b67": "HLG"}


@dataclass(frozen=True, slots=True)
class RenditionPlan:
    kind: str
    codecs: str
    video_range: str
    filters: list[str]
    color_args: list[str]
    crf: int


def rungs_for(kind: str, source_height: int) -> list[int]:
    heights = [height for height in RUNGS[kind] if height <= source_height]
    return heights or [source_height]


def plan_rendition(
    kind: str, probe: SourceProbe, *, max_fps: int, height: int = 1080
) -> RenditionPlan | None:
    """What to encode, or None if this source should not get this kind."""
    scale = [fps_cap_filter(max_fps), f"scale=-2:min({height}\\,ih)"]
    if kind == "hdr":
        transfer = probe.color_transfer
        if not probe.hdr or transfer not in TRANSFER_CODE:
            return None
        return RenditionPlan(
            kind="hdr",
            codecs=f"av01.0.{_level(height)}M.10.0.110.09.{TRANSFER_CODE[transfer]}.09.0",
            video_range=VIDEO_RANGE[transfer],
            filters=[*scale, "format=yuv420p10le"],
            color_args=[
                "-color_primaries", "bt2020", "-color_trc", transfer, "-colorspace", "bt2020nc",
            ],
            crf=30,
        )
    if kind == "av1":
        return RenditionPlan(
            kind="av1",
            codecs=f"av01.0.{_level(height)}M.08",
            video_range="SDR",
            filters=[*scale, *sdr_filters(probe.hdr)],
            color_args=list(BT709_TAGS) if probe.hdr else [],
            crf=34,
        )
    raise ValueError(f"unknown rendition {kind!r}")


def _level(height: int) -> str:
    """AV1 seq_level_idx: 3.0 (04) up to 540p, 3.1 (05) to 720p, 4.0 (08) to 1080p."""
    return "04" if height <= 540 else "05" if height <= 720 else "08"


def rendition_command(
    plan: RenditionPlan, *, source_path: Path, out_dir: Path, ffmpeg_binary: str, max_fps: int
) -> list[str]:
    """One AV1 rendition as fMP4 HLS: `out_dir/index.m3u8`, `init.mp4`, segments."""
    return [
        ffmpeg_binary, "-hide_banner", "-loglevel", "error", "-i", str(source_path),
        "-map", "0:v:0", "-an",
        "-vf", ",".join(plan.filters),
        *plan.color_args,
        "-c:v", "libsvtav1", "-preset", "8", "-crf", str(plan.crf),
        # Two-second groups, so four-second segments cut on keyframes.
        "-g", str(2 * max_fps),
        "-f", "hls", "-hls_time", str(SEGMENT_SECONDS), "-hls_playlist_type", "vod",
        "-hls_segment_type", "fmp4", "-hls_fmp4_init_filename", "init.mp4",
        "-hls_segment_filename", str(out_dir / "seg%05d.m4s"),
        str(out_dir / "index.m3u8"),
    ]


def even_width(width: int, height: int, target_height: int) -> int:
    """`scale=-2:H`'s width: the shape kept, rounded to even."""
    return max(2, round(width * target_height / height / 2) * 2)


def peak_bandwidth(playlist: str, segments: Path) -> tuple[int, int]:
    """(peak, average) bits per second, from the segments and their EXTINFs."""
    durations = [float(m) for m in re.findall(r"#EXTINF:([0-9.]+)", playlist)]
    names = [line for line in playlist.splitlines() if line.endswith(".m4s")]
    rates, total_bytes = [], 0
    for name, seconds in zip(names, durations, strict=False):
        size = (segments / name).stat().st_size
        total_bytes += size
        if seconds > 0:
            rates.append(size * 8 / seconds)
    total_seconds = sum(durations) or 1.0
    return int(max(rates, default=0)), int(total_bytes * 8 / total_seconds)


def _entries(master: str) -> tuple[list[str], list[tuple[str, str]]]:
    """The playlist's head, and its (STREAM-INF, URI) pairs in order."""
    lines = [line for line in master.splitlines() if line.strip()]
    head, entries, index = [], [], 0
    while index < len(lines):
        line = lines[index]
        if line.startswith("#EXT-X-STREAM-INF:") and index + 1 < len(lines):
            entries.append((line, lines[index + 1]))
            index += 2
        else:
            if not entries:
                head.append(line)
            index += 1
    return head, entries


def _rank(uri: str) -> int:
    return ORDER.get(uri.split("/", 1)[0], len(ORDER))


def add_to_master(
    master: str,
    *,
    plan: RenditionPlan,
    rung: int,
    resolution: str,
    bandwidth: tuple[int, int],
) -> str:
    """The master playlist with this rendition listed ahead of the ladder.

    Unchanged if it is already there. It shares the ladder's audio group and
    frame rate, read from the ladder's own first video entry.
    """
    uri = f"{plan.kind}/{rung}/index.m3u8"
    if uri in master:
        return master
    head, entries = _entries(master)
    first = next((info for info, _ in entries if "RESOLUTION=" in info), "")
    audio = re.search(r'AUDIO="([^"]+)"', first)
    rate = re.search(r"FRAME-RATE=([0-9.]+)", first)
    peak, average = bandwidth
    audio_codec = ",mp4a.40.2" if audio else ""
    attributes = [
        f"BANDWIDTH={peak + 128_000}",
        f"AVERAGE-BANDWIDTH={average + 128_000}",
        f"RESOLUTION={resolution}",
        f'CODECS="{plan.codecs}{audio_codec}"',
        f"VIDEO-RANGE={plan.video_range}",
    ]
    if audio:
        attributes.append(f'AUDIO="{audio.group(1)}"')
    if rate:
        attributes.append(f"FRAME-RATE={rate.group(1)}")
    entries.append(("#EXT-X-STREAM-INF:" + ",".join(attributes), uri))
    # Stable: the ladder keeps its own order, and so does each rendition's.
    entries.sort(key=lambda entry: _rank(entry[1]))
    return "\n".join([*head, *(line for entry in entries for line in entry)]) + "\n"


def queue_keep_hdr(job_id, _output_key: str) -> None:
    """The after-ladder hook Keep HDR registers: every finished ladder asks;
    the job itself decides, from the source, whether it is HDR."""
    from app.queue import enqueue_rendition

    enqueue_rendition(job_id, "hdr")


class RenditionBuilder:
    def __init__(
        self,
        store: ObjectStore,
        *,
        prober,
        ffmpeg_binary: str = "ffmpeg",
        timeout_seconds: int = 1770,
        runner=run_process,
    ) -> None:
        self._store = store
        self._prober = prober
        self._ffmpeg = ffmpeg_binary
        self._timeout = timeout_seconds
        self._runner = runner

    def run(self, *, kind: str, source_key: str, hls_key: str, max_fps: int) -> bool:
        """Build `kind` and list it in the master at `hls_key`. False if this
        source does not get one (an SDR source asked for HDR) or already has it."""
        prefix = hls_key.rsplit("/", 1)[0]
        work = Path(tempfile.mkdtemp(prefix=f"flickpond-{kind}-"))
        try:
            master_path = work / "master.m3u8"
            self._store.download_file(key=hls_key, destination=str(master_path))
            master = master_path.read_text(encoding="utf-8")
            if f"{kind}/" in master:
                return False
            source_path = work / "source"
            self._store.download_file(key=source_key, destination=str(source_path))
            probe = self._prober(source_path)
            if plan_rendition(kind, probe, max_fps=max_fps) is None:
                return False
            from app.worker.hls import content_type_for

            for rung in rungs_for(kind, probe.height):
                plan = plan_rendition(kind, probe, max_fps=max_fps, height=rung)
                out_dir = work / kind / str(rung)
                out_dir.mkdir(parents=True)
                result = self._runner(
                    rendition_command(
                        plan, source_path=source_path, out_dir=out_dir,
                        ffmpeg_binary=self._ffmpeg, max_fps=max_fps,
                    ),
                    capture_output=True, text=True, timeout=self._timeout, check=False,
                )
                playlist_path = out_dir / "index.m3u8"
                if result.returncode != 0 or not playlist_path.is_file():
                    detail = (result.stderr or "").strip()[-500:]
                    raise ObjectStoreError(
                        f"FFmpeg failed on the {kind} rendition: {detail}",
                        user_message=UNPROCESSABLE_VIDEO,
                    )
                playlist = playlist_path.read_text(encoding="utf-8")
                for path in sorted(out_dir.iterdir()):
                    self._store.upload_file(
                        key=f"{prefix}/{kind}/{rung}/{path.name}",
                        source=str(path),
                        content_type=content_type_for(path.name),
                    )
                master = add_to_master(
                    master,
                    plan=plan,
                    rung=rung,
                    resolution=f"{even_width(probe.width, probe.height, rung)}x{rung}",
                    bandwidth=peak_bandwidth(playlist, out_dir),
                )
            master_path.write_text(master, encoding="utf-8", newline="\n")
            # Last: until now no player could be sent to a half-uploaded rendition.
            self._store.upload_file(
                key=hls_key, source=str(master_path), content_type=content_type_for("master.m3u8")
            )
            return True
        finally:
            shutil.rmtree(work, ignore_errors=True)
