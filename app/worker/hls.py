"""Building an HLS ladder: several renditions plus a master playlist.

Additive to the existing MP4, never a replacement. `output_key` keeps
pointing at the single-file MP4 that every existing job and every existing
player already uses; `hls_key` points at a master playlist beside it. If
anything in here fails the MP4 still stands, which is why the caller treats
a failure as "no ladder" rather than "the job failed" -- see
`app.worker.tasks.build_ladder_async`, which runs this as its own job after
the MP4 job has already marked the video done.

The ladder is capped by what the source actually contains. A 480p upload
gets two renditions, not six: a 720p rendition of a 480p source costs
encode time and storage to carry no additional detail. It is also capped by
`worker_hls_max_height`, which is what the host can encode in time.
"""

import logging
import shutil
import subprocess
import tempfile
from dataclasses import replace
from pathlib import Path, PurePosixPath
from uuid import UUID

from app.worker.interruption import run as run_process
from app.worker.probe import SourceProbe
from app.worker.storage import (
    BT709_TAGS,
    DEFAULT_MAX_FPS,
    UNPROCESSABLE_VIDEO,
    ObjectStore,
    ObjectStoreError,
    fps_cap_filter,
    sdr_filters,
)

logger = logging.getLogger("app.worker.hls")

# The rungs we offer, shortest first. A source is never given a rung taller
# than itself.
LADDER_HEIGHTS: tuple[int, ...] = (360, 480, 720, 1080, 1440, 2160)

# 480p sits so close to 360p and 720p that a player gains almost nothing from
# it once either neighbour exists, and every rung is a full encode. It stays
# only as a video's top rung, so a 480p upload still plays at its own size.
ONLY_AS_TOP_RUNG: frozenset[int] = frozenset({480})

# Lower rungs are scaled down from this rendition rather than from the source:
# taking 720p and 360p from a 1080p frame is a fraction of the work of taking
# them from a 4K one, and no visible difference at those sizes.
CASCADE_FROM = 1080


# The most each rung may spend, in bits per second. Not a formula: these are
# the conventional ladder figures for H.264 at these heights, and a formula
# fitted to a handful of points would be a worse kind of magic. A ceiling,
# not a target -- see LADDER_CRF.
BITRATES: dict[int, str] = {
    360: "800k",
    480: "1400k",
    720: "2800k",
    1080: "5000k",
    1440: "8000k",
    2160: "14000k",
}
FALLBACK_BITRATE = "800k"

# Each rung encodes to a quality, capped at its bitrate, rather than to a fixed
# bitrate. A fixed rate gives a talking head or a screen recording as many bits
# as a football match; capped quality lets simple footage come out smaller and
# still never lets complex footage exceed the ceiling a player plans around.
# 23 is the MP4's own setting (worker_ffmpeg_crf), so the two match.
LADDER_CRF = 23

PLAYLIST_CONTENT_TYPE = "application/vnd.apple.mpegurl"
SEGMENT_CONTENT_TYPE = "video/mp2t"

MASTER_PLAYLIST_NAME = "master.m3u8"
SEGMENT_SECONDS = 6


def variants_for(height: int, *, max_height: int = 2160) -> tuple[int, ...]:
    """The rungs a source of this height should get, shortest first.

    Never taller than the source, nor than `max_height`. A source shorter
    than the lowest rung still gets exactly one rendition, at its own
    height, rather than being upscaled into the 360p rung or being given no
    ladder at all.
    """
    rungs = {rung for rung in LADDER_HEIGHTS if rung <= min(height, max_height)}
    if not rungs:
        return (height,)
    top = max(rungs)
    return tuple(sorted(r for r in rungs if r == top or r not in ONLY_AS_TOP_RUNG))


def content_type_for(name: str) -> str:
    """A playlist is not a video, and MinIO serves back whatever it was told.

    Getting this wrong is invisible locally -- the bytes are the same either
    way -- and shows up only as a player refusing a manifest it was handed
    under a video content type.
    """
    return PLAYLIST_CONTENT_TYPE if name.endswith(".m3u8") else SEGMENT_CONTENT_TYPE


AUDIO_GROUP = "audio"


def _ladder_filter(heights: tuple[int, ...], max_fps: int, hdr: bool = False) -> str:
    """The filter graph: cap the frame rate (see `fps_cap_filter`), then
    scale every rung.

    Rungs at or above `CASCADE_FROM` are scaled from the source; the ones
    below it from the `CASCADE_FROM` rendition, when the ladder has one.
    Output labels follow `heights`' order -- `[v0out]` is the shortest rung --
    because that is the order the maps and `-var_stream_map` use.
    """
    # Tone-mapped once, before the split, so every rung shares the work.
    fps = ",".join([fps_cap_filter(max_fps), *sdr_filters(hdr)])
    index = {height: i for i, height in enumerate(heights)}
    anchor = CASCADE_FROM if CASCADE_FROM in index else None
    from_source = [h for h in heights if anchor is None or h >= anchor]
    from_anchor = [h for h in heights if anchor is not None and h < anchor]

    parts = [
        f"[0:v]{fps},split={len(from_source)}" + "".join(f"[s{h}]" for h in from_source)
    ]
    for height in from_source:
        if height == anchor and from_anchor:
            branches = [f"[v{index[height]}out]"] + [f"[a{h}]" for h in from_anchor]
            parts.append(
                f"[s{height}]scale=-2:{height},split={len(branches)}" + "".join(branches)
            )
        else:
            parts.append(f"[s{height}]scale=-2:{height}[v{index[height]}out]")
    for height in from_anchor:
        parts.append(f"[a{height}]scale=-2:{height}[v{index[height]}out]")
    return ";".join(parts)


def _double(rate: str) -> str:
    """"5000k" -> "10000k": the buffer, two seconds of the ceiling."""
    return f"{int(rate.removesuffix('k')) * 2}k"


def build_ladder_command(
    source_path: Path,
    output_dir: Path,
    *,
    heights: tuple[int, ...],
    ffmpeg_binary: str = "ffmpeg",
    preset: str = "veryfast",
    max_fps: int = DEFAULT_MAX_FPS,
    hdr: bool = False,
) -> list[str]:
    """One invocation producing every rendition and the master playlist.

    Built from `heights` rather than hardcoding three of everything: the
    `split` counts, the per-rendition maps and `-var_stream_map` all have to
    agree, and a mismatch between them is an FFmpeg error rather than a
    quietly wrong output -- which is the good case, and the reason this
    function is what the tests cover.
    """
    count = len(heights)
    filter_complex = _ladder_filter(heights, max_fps, hdr)

    command = [
        ffmpeg_binary,
        "-hide_banner",
        "-loglevel",
        "error",
        "-i",
        str(source_path),
        "-filter_complex",
        filter_complex,
    ]

    for index, height in enumerate(heights):
        ceiling = BITRATES.get(height, FALLBACK_BITRATE)
        command += [
            "-map",
            f"[v{index}out]",
            f"-c:v:{index}",
            "libx264",
            "-preset",
            preset,
            f"-crf:v:{index}",
            str(LADDER_CRF),
            # The ceiling, and a two-second buffer to average it over: x264
            # may burst within the buffer but never past the rate across it.
            # It also becomes the rung's BANDWIDTH in the master playlist,
            # which is what a player chooses rungs by.
            f"-maxrate:v:{index}",
            ceiling,
            f"-bufsize:v:{index}",
            _double(ceiling),
        ]

    # The audio is encoded once, as its own rendition every video rung points
    # at (an HLS audio group), instead of once per rung -- the same soundtrack
    # six times was six AAC encodes and six copies in storage. It lands in
    # v{count}/, the next index after the video rungs.
    #
    # `a:0?` rather than `a:0`: the trailing question mark makes the mapping
    # optional, so a source with no audio track does not fail here. It fails
    # a moment later in `-var_stream_map`, which names an audio stream that
    # does not exist -- deliberately left as a ladder failure rather than
    # special-cased, because the MP4 fallback already covers it and guessing
    # at a silent audio track would be worse.
    if hdr:
        command += BT709_TAGS
    command += ["-map", "a:0?", "-c:a", "aac", "-b:a", "128k"]

    video = " ".join(f"v:{index},agroup:{AUDIO_GROUP}" for index in range(count))
    var_stream_map = f"{video} a:0,agroup:{AUDIO_GROUP}"
    command += [
        "-f",
        "hls",
        "-hls_time",
        str(SEGMENT_SECONDS),
        "-hls_playlist_type",
        "vod",
        "-hls_segment_filename",
        str(output_dir / "v%v" / "seg%05d.ts"),
        "-master_pl_name",
        MASTER_PLAYLIST_NAME,
        "-var_stream_map",
        var_stream_map,
        "-y",
        str(output_dir / "v%v" / "index.m3u8"),
    ]
    return command


def label_frame_rates(master: str, frame_rate: float) -> str:
    """Add FRAME-RATE to every video rung of a master playlist.

    FFmpeg leaves it out, and it is how a player knows a rung is 60 fps: the
    quality menu says "1080p60" from it. Every rung shares one rate -- the
    ladder caps the source's rate once, before the split -- so one number
    serves all. The audio-only variant has no RESOLUTION and gets none.
    """
    lines = []
    for line in master.splitlines():
        if (
            line.startswith("#EXT-X-STREAM-INF:")
            and "RESOLUTION=" in line
            and "FRAME-RATE=" not in line
        ):
            line = f"{line},FRAME-RATE={frame_rate:.3f}"
        lines.append(line)
    return "\n".join(lines) + "\n"


class HlsLadderBuilder:
    """Produces a ladder from a source object and uploads it.

    It used to take only a local path, sharing the MP4 job's download. As
    its own job it fetches the source again: one extra read of the upload,
    traded for the MP4 no longer waiting on every rendition.
    """

    def __init__(
        self,
        store: ObjectStore,
        *,
        output_prefix: str,
        ffmpeg_binary: str = "ffmpeg",
        preset: str = "veryfast",
        timeout_seconds: int = 870,
        max_height: int = 2160,
        runner=run_process,
        prober=None,
    ) -> None:
        self._store = store
        self._output_prefix = output_prefix.strip("/")
        self._ffmpeg_binary = ffmpeg_binary
        self._preset = preset
        self._timeout_seconds = timeout_seconds
        self._max_height = max_height
        self._runner = runner
        self._prober = prober

    def prefix_for(self, job_id: UUID) -> str:
        return f"{self._output_prefix}/{job_id}/hls"

    def run(
        self,
        *,
        job_id: UUID,
        source_key: str,
        probe: SourceProbe,
        max_fps: int = DEFAULT_MAX_FPS,
    ) -> str:
        """Download `source_key`, build its ladder, return the master's key."""
        temp_dir = Path(tempfile.mkdtemp(prefix="flickpond-ladder-src-"))
        try:
            source_path = temp_dir / "source"
            self._store.download_file(key=source_key, destination=str(source_path))
            probe = self._with_file_facts(job_id, source_path, probe)
            return self.build(
                job_id=job_id, source_path=source_path, probe=probe, max_fps=max_fps
            )
        finally:
            shutil.rmtree(temp_dir, ignore_errors=True)

    def _with_file_facts(self, job_id: UUID, source_path: Path, probe: SourceProbe) -> SourceProbe:
        """Add what the row does not store -- HDR, frame rate -- from the file.

        The row stores only size and duration, so the ladder asks the file. A
        failed probe reads as SDR of unknown rate: the output is still 8-bit
        and playable, just not tone-mapped, and its rungs carry no FRAME-RATE.
        """
        if self._prober is None:
            return probe
        try:
            found = self._prober(source_path)
        except Exception:
            logger.warning("job %s: could not read the colour transfer; treating as SDR", job_id)
            return probe
        return replace(probe, hdr=probe.hdr or found.hdr, frame_rate=found.frame_rate)

    def discard(self, job_id: UUID) -> None:
        """Remove a ladder nobody will record -- see `build_ladder_async`."""
        self._store.delete_prefix(self.prefix_for(job_id) + "/")

    def build(
        self,
        *,
        job_id: UUID,
        source_path: Path,
        probe: SourceProbe,
        max_fps: int = DEFAULT_MAX_FPS,
    ) -> str:
        """Build and upload the ladder, returning the master playlist's key."""
        heights = variants_for(probe.height, max_height=self._max_height)
        logger.info(
            "job %s: building HLS ladder %s from a %dx%d source",
            job_id,
            heights,
            probe.width,
            probe.height,
        )

        work_dir = Path(tempfile.mkdtemp(prefix="flickpond-hls-"))
        try:
            # FFmpeg's %v expands to the variant index but does not create the
            # directories it expands into. One more than the rungs: the shared
            # audio rendition is a variant of its own.
            for index in range(len(heights) + 1):
                (work_dir / f"v{index}").mkdir(parents=True, exist_ok=True)

            command = build_ladder_command(
                source_path,
                work_dir,
                heights=heights,
                ffmpeg_binary=self._ffmpeg_binary,
                preset=self._preset,
                max_fps=max_fps,
                hdr=probe.hdr,
            )
            try:
                result = self._runner(
                    command,
                    capture_output=True,
                    text=True,
                    timeout=self._timeout_seconds,
                    check=False,
                )
            except subprocess.TimeoutExpired as exc:
                raise ObjectStoreError(
                    f"HLS ladder timed out after {self._timeout_seconds}s",
                    user_message=UNPROCESSABLE_VIDEO,
                ) from exc

            if result.returncode != 0:
                detail = "\n".join((result.stderr or "").strip().splitlines()[-5:])[:1000]
                raise ObjectStoreError(
                    f"FFmpeg failed building the HLS ladder: {detail}",
                    user_message=UNPROCESSABLE_VIDEO,
                )

            master = work_dir / MASTER_PLAYLIST_NAME
            if not master.is_file():
                raise ObjectStoreError(
                    "FFmpeg produced no master playlist",
                    user_message=UNPROCESSABLE_VIDEO,
                )
            if probe.frame_rate:
                rate = min(float(max_fps), probe.frame_rate)
                # A fresh mkdtemp directory and a constant file name: no part
                # of this path comes from a request.
                master.write_text(  # NOSONAR
                    label_frame_rates(master.read_text(encoding="utf-8"), rate),
                    encoding="utf-8",
                )

            prefix = self.prefix_for(job_id)
            for path in sorted(work_dir.rglob("*")):
                if not path.is_file():
                    continue
                relative = PurePosixPath(path.relative_to(work_dir).as_posix())
                self._store.upload_file(
                    key=f"{prefix}/{relative}",
                    source=str(path),
                    content_type=content_type_for(path.name),
                )

            return f"{prefix}/{MASTER_PLAYLIST_NAME}"
        finally:
            shutil.rmtree(work_dir, ignore_errors=True)
