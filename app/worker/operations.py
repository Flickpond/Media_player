"""One edit job: download, validate, one FFmpeg command, upload.

Which filters an operation adds lives in `app.worker.edit_ops`; the order they
run in is `app.services.media_rules.OPERATION_ORDER`.
"""

import logging
import shutil
import subprocess
import tempfile
from pathlib import Path, PurePosixPath
from uuid import UUID

from pydantic import ValidationError

from app.schemas.edit import EditRequest
from app.services.media_rules import OPERATION_RULES, Frame
from app.worker.edit_ops import filter_chain
from app.worker.interruption import run as run_process
from app.worker.storage import (
    BT709_TAGS,
    DEFAULT_MAX_FPS,
    UNPROCESSABLE_VIDEO,
    ObjectStore,
    ObjectStoreError,
    ProcessingResult,
    fps_cap_filter,
    sdr_filters,
)
from app.worker.validation import validate_clip, validate_crop, validate_edit_rules

INVALID_EDIT = "the edit request is invalid; please submit it again"

logger = logging.getLogger("app.worker.operations")


def _params(item) -> dict:
    return item.params.model_dump(mode="python")


def build_edit_command(
    request: EditRequest,
    *,
    ffmpeg_binary: str,
    source_path: Path,
    output_path: Path,
    preset: str,
    crf: int,
    max_fps: int = DEFAULT_MAX_FPS,
    hdr: bool = False,
    frame: Frame | None = None,
) -> list[str]:
    command = [ffmpeg_binary, "-hide_banner", "-loglevel", "error"]

    clip = request.operation("clip")
    if clip is not None:
        params = _params(clip)
        command.extend(["-ss", str(params["start"]), "-to", str(params["end"])])
    command.extend(["-i", str(source_path)])

    operations = [(item.operation, _params(item)) for item in request.operations]
    chain = filter_chain(operations, frame or Frame(width=None, height=None))
    filters, audio_filters = chain.video, chain.audio

    convert = request.operation("convert")
    output_format = convert.params.format if convert is not None else "mp4"
    if output_format == "mp3":
        # Picture operations cannot change an audio-only result. The API
        # refuses them with MP3 first; a stored request that has them anyway
        # is caught here rather than silently ignored. An operation that is
        # not a picture change but still has video filters -- speed retimes
        # the frames as well as the sound -- keeps only its audio ones.
        if any(OPERATION_RULES[name].changes_picture for name, _ in operations):
            raise ObjectStoreError(
                "video filters cannot produce an audio-only output",
                user_message="edits that change the picture cannot be combined with MP3 conversion",
            )
        command.append("-vn")
        if audio_filters:
            command.extend(["-af", ",".join(audio_filters)])
        command.extend(["-c:a", "libmp3lame"])
    elif output_format == "mkv" and not filters and not audio_filters:
        command.extend(["-c", "copy"])
    else:
        # Re-encoding anyway, so the frame-rate cap rides along for free. A
        # stream copy (the remux above) keeps the source's frames untouched.
        # The tone-map sits between the geometry and the colour: crop and
        # scale run on the source as decoded, colour on the standard 8-bit
        # picture the viewer and the editor's preview both see.
        command.extend(
            [
                "-vf",
                ",".join(
                    [
                        fps_cap_filter(max_fps),
                        *chain.before_tone_map,
                        *sdr_filters(hdr),
                        *chain.after_tone_map,
                    ]
                ),
            ]
        )
        if audio_filters:
            command.extend(["-af", ",".join(audio_filters)])
        if hdr:
            command.extend(BT709_TAGS)
        command.extend(
            ["-c:v", "libx264", "-preset", preset, "-crf", str(crf), "-c:a", "aac"]
        )
        if output_format == "mp4":
            command.extend(["-movflags", "+faststart"])

    command.extend(["-y", str(output_path)])
    return command


class EditProcessor:
    def __init__(
        self,
        store: ObjectStore,
        operations: list[dict],
        *,
        output_prefix: str,
        ffmpeg_binary: str = "ffmpeg",
        preset: str = "veryfast",
        crf: int = 23,
        timeout_seconds: int = 870,
        runner=run_process,
        prober,
        thumbnailer=None,
    ) -> None:
        self._store = store
        self._operations = operations
        # The same poster frame an upload gets (`app.worker.thumbnail`), taken
        # from the edited result so the library shows the crop, not the
        # original. Without it every edit's card was blank (23 Oct 2026).
        self._thumbnailer = thumbnailer
        self._output_prefix = output_prefix.strip("/")
        self._ffmpeg_binary = ffmpeg_binary
        self._preset = preset
        self._crf = crf
        self._timeout_seconds = timeout_seconds
        self._runner = runner
        self._prober = prober

    def _thumbnail(self, job_id: UUID, output_path: Path, produced, temp_dir: Path) -> str | None:
        """The edit's poster frame's key, or None. Never fails the edit."""
        if self._thumbnailer is None:
            return None
        from app.worker.thumbnail import THUMBNAIL_CONTENT_TYPE, THUMBNAIL_FILENAME

        try:
            frame = self._thumbnailer(output_path, temp_dir / THUMBNAIL_FILENAME, produced)
            key = f"{self._output_prefix}/{job_id}/{THUMBNAIL_FILENAME}"
            self._store.upload_file(
                key=key, source=str(frame), content_type=THUMBNAIL_CONTENT_TYPE
            )
            return key
        except Exception:
            logger.warning("job %s: no thumbnail for the edit", job_id, exc_info=True)
            return None

    def _request(self) -> EditRequest:
        try:
            return EditRequest.model_validate({"operations": self._operations})
        except ValidationError as exc:
            raise ObjectStoreError(
                "invalid persisted edit operations", user_message=INVALID_EDIT
            ) from exc

    def _is_hdr(self, job_id: UUID, source_path: Path, probe) -> bool:
        """Whether the source is HDR, from the validation probe when there was
        one. A failed probe reads as SDR: still 8-bit and playable."""
        if probe is not None:
            return probe.hdr
        try:
            return self._prober(source_path).hdr
        except Exception:
            logger.warning("job %s: could not read the colour transfer; treating as SDR", job_id)
            return False

    def _probe_output(self, *, job_id: UUID, output_path: Path):
        """The finished edit's size, or None. The edit is already uploaded,
        so a probe failure costs the ladder, not the job."""
        try:
            return self._prober(output_path)
        except Exception:
            logger.exception("job %s: could not probe the edit output; no ladder", job_id)
            return None

    def run(
        self, *, job_id: UUID, source_key: str, max_fps: int = DEFAULT_MAX_FPS
    ) -> ProcessingResult:
        if not self._store.object_exists(source_key):
            raise ObjectStoreError(
                f"source object missing from storage: {source_key}",
                user_message="the source video is no longer in storage; choose another video",
            )

        request = self._request()
        convert = request.operation("convert")
        extension = convert.params.format if convert is not None else "mp4"
        content_type = {
            "mp4": "video/mp4",
            "mkv": "video/x-matroska",
            "mp3": "audio/mpeg",
        }[extension]
        stem = PurePosixPath(source_key).stem or str(job_id)
        output_key = f"{self._output_prefix}/{job_id}/{stem}.{extension}"

        temp_dir = Path(tempfile.mkdtemp(prefix="flickpond-edit-"))
        source_path = temp_dir / "source"
        output_path = temp_dir / f"output.{extension}"
        try:
            self._store.download_file(key=source_key, destination=str(source_path))

            crop = request.operation("crop")
            clip = request.operation("clip")
            probe = None
            # Anything beyond a format change is checked against the real
            # file before FFmpeg starts.
            if any(item.operation != "convert" for item in request.operations):
                probe = self._prober(source_path)
                try:
                    if clip is not None:
                        validate_clip(_params(clip), probe)
                    if crop is not None:
                        validate_crop(_params(crop), probe)
                    # After crop: scale is judged against the frame it is
                    # applied to, which a valid crop has already defined.
                    validate_edit_rules(
                        [(item.operation, _params(item)) for item in request.operations], probe
                    )
                except ValueError as exc:
                    raise ObjectStoreError("edit validation failed", user_message=str(exc)) from exc

            command = build_edit_command(
                request,
                ffmpeg_binary=self._ffmpeg_binary,
                source_path=source_path,
                output_path=output_path,
                preset=self._preset,
                crf=self._crf,
                max_fps=max_fps,
                hdr=self._is_hdr(job_id, source_path, probe),
                frame=Frame(width=probe.width, height=probe.height) if probe else None,
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
                    f"editing timed out after {self._timeout_seconds} seconds",
                    user_message="the edit took too long; try a shorter or smaller video",
                ) from exc
            if result.returncode != 0:
                detail = "\n".join((result.stderr or "").strip().splitlines()[-5:])[:1000]
                raise ObjectStoreError(
                    f"FFmpeg edit failed: {detail or 'no FFmpeg error output'}",
                    user_message=UNPROCESSABLE_VIDEO,
                )
            if not output_path.is_file():
                raise ObjectStoreError(
                    "FFmpeg completed without producing an edit output",
                    user_message=UNPROCESSABLE_VIDEO,
                )

            self._store.upload_file(
                key=output_key,
                source=str(output_path),
                content_type=content_type,
            )
            # The edit's own size, not the source's: a crop or a scale changes
            # it, and this is what the ladder is cut from and what the page
            # offers options against. An MP3 has no picture, so no ladder.
            if extension == "mp3":
                return ProcessingResult(output_key=output_key)
            produced = self._probe_output(job_id=job_id, output_path=output_path)
            if produced is None:
                return ProcessingResult(output_key=output_key)
            return ProcessingResult(
                output_key=output_key,
                ladder_pending=True,
                width=produced.width,
                height=produced.height,
                duration_seconds=produced.duration_seconds,
                thumbnail_key=self._thumbnail(job_id, output_path, produced, temp_dir),
            )
        finally:
            shutil.rmtree(temp_dir, ignore_errors=True)
