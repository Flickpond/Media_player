"""Extra AV1 renditions added to a finished ladder: Keep HDR, and popular videos.

The ladder every video gets is H.264, standard range: it plays everywhere.
Two kinds of video earn a small AV1 ladder on top of it (1080p, 720p, 360p),
listed in the same master playlist:

* **hdr** -- an HDR upload, kept HDR (10-bit, PQ or HLG as recorded) when
  `WORKER_KEEP_HDR` is on. The playlist marks it `VIDEO-RANGE=PQ`/`HLG`, and
  hls.js (1.7) offers it only on a screen that reports `dynamic-range: high`;
  every other screen keeps playing the tone-mapped ladder.
* **av1** -- a video enough people have watched (`AV1_POPULAR_VIEWS`): the
  same picture as the ladder for fewer bytes, where the browser decodes AV1.
  Only for sources up to 1080p: above that the H.264 ladder goes higher than
  AV1 does here, and a 4K screen would be moved down to 1080p to save bytes.

Each is its own job on the ladder queue, after the ladder is ready, so it can
neither delay nor break the video: if it fails, the video simply has no extra
rendition. The master playlist is uploaded last, so a player never sees a
rendition whose segments are not there yet.

**What makes a player use them** (hls.js 1.7, `getStartCodecTier`): it picks
one codec family to start in and stays there. It prefers AV1 to H.264, but
skips any family whose *cheapest* entry costs more than its first bandwidth
estimate -- roughly the cheapest entry in the whole playlist. On flickpond.com
an uncapped AV1 540p peaked at 5.8 Mbps against H.264 360p's 1 Mbps, and no
browser ever started in AV1. Hence the 360p rung, and the caps: each rung is
held below the H.264 ceiling at its height (CAP_SHARE), so the AV1 family's
cheapest entry is the cheapest in the playlist. The entries are listed first
as well -- HDR, then AV1, then the ladder.
"""

import contextlib
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
# The same heights as the H.264 ladder, so each rung has a ceiling to sit under.
RUNGS = {"hdr": (1080, 720, 360), "av1": (1080, 720, 360)}
# The popular rendition is for sources the H.264 ladder does not take above it.
AV1_MAX_SOURCE_HEIGHT = 1080
# Each rung's ceiling, as a share of the H.264 ceiling at the same height. On
# the 4K HDR demo clip, AV1 at CRF 30 capped at 0.6 matched the H.264 ladder's
# PSNR at 1080p and 720p on 75-85% of its bytes; HDR's 10 bits get more.
CAP_SHARE = {"hdr": 0.75, "av1": 0.6}
# The bottom rung's ceiling, whatever the share says. hls.js starts in a family
# only if its cheapest entry fits max(cheapest entry anywhere, 500 kbps -- its
# default first estimate), and simple footage can bring H.264 360p well under
# 500 kbps. So the bottom rung is held under 500 kbps itself, audio included:
# the measured peak can reach 1.25x the cap (a one-second buffer averaged into
# four-second segments), and add_to_master adds 128 kbps for the audio.
START_RUNG_HEIGHT = 360
START_CEILING = 296_000
CRF = 30
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
    # Bits per second, and the size of the buffer it is averaged over.
    maxrate: int


def rungs_for(kind: str, source_height: int) -> list[int]:
    heights = [height for height in RUNGS[kind] if height <= source_height]
    return heights or [source_height]


def ceiling(kind: str, height: int) -> int:
    """The most a rung of `kind` may spend, in bits per second."""
    from app.worker.hls import BITRATES, FALLBACK_BITRATE

    h264 = int(BITRATES.get(height, FALLBACK_BITRATE).removesuffix("k")) * 1000
    shared = int(h264 * CAP_SHARE[kind])
    return min(shared, START_CEILING) if height <= START_RUNG_HEIGHT else shared


def plan_rendition(
    kind: str, probe: SourceProbe, *, max_fps: int, height: int = 1080
) -> RenditionPlan | None:
    """What to encode, or None if this source should not get this kind."""
    scale = [fps_cap_filter(max_fps), f"scale=-2:min({height}\\,ih)"]
    maxrate = ceiling(kind, height) if kind in CAP_SHARE else 0
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
            crf=CRF,
            maxrate=maxrate,
        )
    if kind == "av1":
        if probe.height > AV1_MAX_SOURCE_HEIGHT:
            return None
        return RenditionPlan(
            kind="av1",
            codecs=f"av01.0.{_level(height)}M.08",
            video_range="SDR",
            filters=[*scale, *sdr_filters(probe.hdr)],
            color_args=list(BT709_TAGS) if probe.hdr else [],
            crf=CRF,
            maxrate=maxrate,
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
        # Capped CRF (SVT-AV1 reports it as such): quality, but never past the
        # ceiling over a one-second buffer -- which is what keeps the measured
        # peak, the BANDWIDTH a player chooses by, under the H.264 rung's.
        "-maxrate", f"{plan.maxrate // 1000}k", "-bufsize", f"{plan.maxrate // 1000}k",
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
        master_lock=lambda _hls_key: contextlib.nullcontext(),
    ) -> None:
        self._store = store
        self._prober = prober
        self._ffmpeg = ffmpeg_binary
        self._timeout = timeout_seconds
        self._runner = runner
        # Held only while the master is re-read, extended and uploaded: the HDR
        # and AV1 jobs for one video can run at once on two workers, and each
        # used to upload the master it had read minutes earlier -- on
        # flickpond.com the AV1 job's upload dropped the HDR entries.
        self._master_lock = master_lock

    def run(self, *, kind: str, source_key: str, hls_key: str, max_fps: int) -> bool:
        """Build `kind` and list it in the master at `hls_key`. False if this
        source does not get one (an SDR source asked for HDR) or already has it."""
        prefix = hls_key.rsplit("/", 1)[0]
        # `kind` arrives as a queue argument and names directories below: only
        # the two this module builds.
        if kind not in KINDS:
            raise ValueError(f"unknown rendition {kind!r}")
        work = Path(tempfile.mkdtemp(prefix="flickpond-rendition-"))
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

            listed = []
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
                listed.append(
                    {
                        "plan": plan,
                        "rung": rung,
                        "resolution": f"{even_width(probe.width, probe.height, rung)}x{rung}",
                        "bandwidth": peak_bandwidth(playlist, out_dir),
                    }
                )
            # Last: until now no player could be sent to a half-uploaded rendition.
            with self._master_lock(hls_key):
                # Read again: another rendition may have been listed meanwhile.
                self._store.download_file(key=hls_key, destination=str(master_path))
                master = master_path.read_text(encoding="utf-8")
                for entry in listed:
                    master = add_to_master(master, **entry)
                # mkdtemp's directory and a constant name; `kind` checked above.
                master_path.write_text(master, encoding="utf-8", newline="\n")  # NOSONAR
                self._store.upload_file(
                    key=hls_key,
                    source=str(master_path),
                    content_type=content_type_for("master.m3u8"),
                )
            return True
        finally:
            shutil.rmtree(work, ignore_errors=True)
