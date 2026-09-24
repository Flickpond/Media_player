"""Building an HLS ladder: several renditions plus a master playlist.

Additive to the existing MP4, never a replacement. `output_key` keeps
pointing at the single-file MP4 that every existing job and every existing
player already uses; `hls_key` points at a master playlist beside it. If
anything in here fails the MP4 still stands, which is why the caller treats
a failure as "no ladder" rather than "the job failed" -- see
`FfmpegProcessor.run`.

The ladder is capped by what the source actually contains. A 480p upload
gets two renditions, not three: a 720p rendition of a 480p source costs
encode time and storage to carry no additional detail.
"""

import logging
import shutil
import subprocess
import tempfile
from pathlib import Path, PurePosixPath
from uuid import UUID

from app.worker.probe import SourceProbe
from app.worker.storage import UNPROCESSABLE_VIDEO, ObjectStore, ObjectStoreError

logger = logging.getLogger("app.worker.hls")

# The rungs we offer, shortest first. A source is never given a rung taller
# than itself.
LADDER_HEIGHTS: tuple[int, ...] = (360, 480, 720)

# Video bitrate per rung. Not a formula: these are the conventional ladder
# figures for H.264 at these heights, and a formula fitted to three points
# would be a worse kind of magic.
BITRATES: dict[int, str] = {360: "800k", 480: "1400k", 720: "2800k"}
FALLBACK_BITRATE = "800k"

PLAYLIST_CONTENT_TYPE = "application/vnd.apple.mpegurl"
SEGMENT_CONTENT_TYPE = "video/mp2t"

MASTER_PLAYLIST_NAME = "master.m3u8"
SEGMENT_SECONDS = 6


def variants_for(height: int) -> tuple[int, ...]:
    """The rungs a source of this height should get, shortest first.

    Never taller than the source. A source shorter than the lowest rung
    still gets exactly one rendition, at its own height, rather than being
    upscaled into the 360p rung or being given no ladder at all.
    """
    rungs = {rung for rung in LADDER_HEIGHTS if rung <= height}
    return tuple(sorted(rungs)) if rungs else (height,)


def content_type_for(name: str) -> str:
    """A playlist is not a video, and MinIO serves back whatever it was told.

    Getting this wrong is invisible locally -- the bytes are the same either
    way -- and shows up only as a player refusing a manifest it was handed
    under a video content type.
    """
    return PLAYLIST_CONTENT_TYPE if name.endswith(".m3u8") else SEGMENT_CONTENT_TYPE


def build_ladder_command(
    source_path: Path,
    output_dir: Path,
    *,
    heights: tuple[int, ...],
    ffmpeg_binary: str = "ffmpeg",
    preset: str = "veryfast",
) -> list[str]:
    """One invocation producing every rendition and the master playlist.

    Built from `heights` rather than hardcoding three of everything: the
    `split` count, the per-rendition maps and `-var_stream_map` all have to
    agree, and a mismatch between them is an FFmpeg error rather than a
    quietly wrong output -- which is the good case, and the reason this
    function is what the tests cover.
    """
    count = len(heights)

    labels = "".join(f"[v{index}]" for index in range(count))
    scales = ";".join(
        f"[v{index}]scale=-2:{height}[v{index}out]" for index, height in enumerate(heights)
    )
    filter_complex = f"[0:v]split={count}{labels};{scales}"

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
        command += [
            "-map",
            f"[v{index}out]",
            f"-c:v:{index}",
            "libx264",
            "-preset",
            preset,
            f"-b:v:{index}",
            BITRATES.get(height, FALLBACK_BITRATE),
        ]

    # `a:0?` rather than `a:0`: the trailing question mark makes the mapping
    # optional, so a source with no audio track does not fail here. It fails
    # a moment later in `-var_stream_map`, which names an audio stream that
    # does not exist -- deliberately left as a ladder failure rather than
    # special-cased, because the MP4 fallback already covers it and guessing
    # at a silent audio track would be worse.
    for _ in heights:
        command += ["-map", "a:0?"]
    command += ["-c:a", "aac", "-b:a", "128k"]

    var_stream_map = " ".join(f"v:{index},a:{index}" for index in range(count))
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


class HlsLadderBuilder:
    """Produces a ladder from an already-downloaded source and uploads it.

    Takes the local path rather than an object key because the caller has
    already downloaded the source to transcode it -- fetching it a second
    time would double the object-store traffic of every upload to save
    passing one argument.
    """

    def __init__(
        self,
        store: ObjectStore,
        *,
        output_prefix: str,
        ffmpeg_binary: str = "ffmpeg",
        preset: str = "veryfast",
        timeout_seconds: int = 870,
        runner=subprocess.run,
    ) -> None:
        self._store = store
        self._output_prefix = output_prefix.strip("/")
        self._ffmpeg_binary = ffmpeg_binary
        self._preset = preset
        self._timeout_seconds = timeout_seconds
        self._runner = runner

    def prefix_for(self, job_id: UUID) -> str:
        return f"{self._output_prefix}/{job_id}/hls"

    def build(self, *, job_id: UUID, source_path: Path, probe: SourceProbe) -> str:
        """Build and upload the ladder, returning the master playlist's key."""
        heights = variants_for(probe.height)
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
            # directories it expands into.
            for index in range(len(heights)):
                (work_dir / f"v{index}").mkdir(parents=True, exist_ok=True)

            command = build_ladder_command(
                source_path,
                work_dir,
                heights=heights,
                ffmpeg_binary=self._ffmpeg_binary,
                preset=self._preset,
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
