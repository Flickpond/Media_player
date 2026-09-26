"""Per-job editing registry and the fixed clip -> crop -> scale -> convert pipeline."""

import shutil
import subprocess
import tempfile
from pathlib import Path, PurePosixPath
from uuid import UUID

from pydantic import ValidationError

from app.schemas.edit import EditRequest
from app.worker.storage import (
    UNPROCESSABLE_VIDEO,
    ObjectStore,
    ObjectStoreError,
    ProcessingResult,
)
from app.worker.validation import validate_clip, validate_crop

INVALID_EDIT = "the edit request is invalid; please submit it again"


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
) -> list[str]:
    command = [ffmpeg_binary, "-hide_banner", "-loglevel", "error"]

    clip = request.operation("clip")
    if clip is not None:
        params = _params(clip)
        command.extend(["-ss", str(params["start"]), "-to", str(params["end"])])
    command.extend(["-i", str(source_path)])

    filters: list[str] = []
    crop = request.operation("crop")
    if crop is not None:
        params = _params(crop)
        filters.append(f"crop={params['w']}:{params['h']}:{params['x']}:{params['y']}")
    downscale = request.operation("downscale")
    upscale = request.operation("upscale")
    if downscale is not None:
        filters.append(f"scale=-2:{downscale.params.height}")
    elif upscale is not None:
        filters.append(f"scale=-2:{upscale.params.height}:flags=lanczos")

    convert = request.operation("convert")
    output_format = convert.params.format if convert is not None else "mp4"
    if output_format == "mp3":
        # Video-only operations cannot change an audio-only result. The API
        # accepts the common clip+MP3 case; crop/scale combinations are caught
        # here as invalid persisted requests rather than silently ignored.
        if filters:
            raise ObjectStoreError(
                "video filters cannot produce an audio-only output",
                user_message="crop and scale cannot be combined with MP3 conversion",
            )
        command.extend(["-vn", "-c:a", "libmp3lame"])
    elif output_format == "mkv" and not filters:
        command.extend(["-c", "copy"])
    else:
        if filters:
            command.extend(["-vf", ",".join(filters)])
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
        runner=subprocess.run,
        prober,
    ) -> None:
        self._store = store
        self._operations = operations
        self._output_prefix = output_prefix.strip("/")
        self._ffmpeg_binary = ffmpeg_binary
        self._preset = preset
        self._crf = crf
        self._timeout_seconds = timeout_seconds
        self._runner = runner
        self._prober = prober

    def _request(self) -> EditRequest:
        try:
            return EditRequest.model_validate({"operations": self._operations})
        except ValidationError as exc:
            raise ObjectStoreError(
                "invalid persisted edit operations", user_message=INVALID_EDIT
            ) from exc

    def run(self, *, job_id: UUID, source_key: str) -> ProcessingResult:
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
            if crop is not None or clip is not None:
                probe = self._prober(source_path)
                try:
                    if clip is not None:
                        validate_clip(_params(clip), probe)
                    if crop is not None:
                        validate_crop(_params(crop), probe)
                except ValueError as exc:
                    raise ObjectStoreError("edit validation failed", user_message=str(exc)) from exc

            command = build_edit_command(
                request,
                ffmpeg_binary=self._ffmpeg_binary,
                source_path=source_path,
                output_path=output_path,
                preset=self._preset,
                crf=self._crf,
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
            return ProcessingResult(output_key=output_key)
        finally:
            shutil.rmtree(temp_dir, ignore_errors=True)
