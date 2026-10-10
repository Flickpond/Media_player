"""The uploader's frame-rate choice, from the job row to every encoder.

Every encode is capped at 30 fps unless the uploader chose to keep 60: a
phone's 60 fps costs twice the work, so it is never the default. These pin
that the choice reaches the MP4, the ladder and any edit made from the
video -- a cap that one encoder forgot would be invisible until someone
compared frame rates.
"""

from contextlib import asynccontextmanager
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

from app.models.job import HlsStatus, JobStatus
from app.repositories.uploads import create_upload_job
from app.schemas.edit import EditRequest
from app.worker import tasks
from app.worker.hls import build_ladder_command
from app.worker.operations import build_edit_command
from app.worker.storage import FfmpegProcessor, ProcessingResult, fps_cap_filter
from app.worker.tasks import build_ladder_async, process_job_async

SIXTY = "fps=fps=min(60\\,source_fps)"
THIRTY = "fps=fps=min(30\\,source_fps)"


def test_the_cap_never_raises_a_frame_rate():
    """`min` against the source's own rate: a 24 fps film stays 24."""
    assert fps_cap_filter(60) == SIXTY
    assert "source_fps" in fps_cap_filter(30)


# --- each encoder honours the cap it is given --------------------------------


class _Store:
    def object_exists(self, key):
        return True

    def download_file(self, *, key, destination):
        Path(destination).write_bytes(b"src")

    def upload_file(self, *, key, source, content_type="video/mp4"):
        pass


def test_the_mp4_keeps_60_fps_when_the_uploader_asked():
    commands: list[list[str]] = []

    def runner(command, **_kwargs):
        commands.append(command)
        Path(command[-1]).write_bytes(b"mp4")
        return SimpleNamespace(returncode=0, stderr="")

    processor = FfmpegProcessor(_Store(), output_prefix="outputs", runner=runner)
    processor.run(job_id=uuid4(), source_key="uploads/a/clip.mp4", max_fps=60)

    assert commands[-1][commands[-1].index("-vf") + 1].startswith(SIXTY)


def test_the_ladder_keeps_60_fps_when_the_uploader_asked():
    command = build_ladder_command(Path("/s"), Path("/o"), heights=(360, 1080), max_fps=60)

    assert command[command.index("-filter_complex") + 1].startswith(f"[0:v]{SIXTY},")


def test_an_edit_that_re_encodes_keeps_60_fps_when_its_video_did():
    request = EditRequest.model_validate(
        {"operations": [{"operation": "downscale", "params": {"height": 720}}]}
    )
    command = build_edit_command(
        request,
        ffmpeg_binary="ffmpeg",
        source_path=Path("/s"),
        output_path=Path("/o.mp4"),
        preset="veryfast",
        crf=23,
        max_fps=60,
    )

    assert command[command.index("-vf") + 1].startswith(SIXTY)


def test_a_remux_is_left_untouched():
    """A stream copy re-encodes nothing, so there is nothing to cap."""
    request = EditRequest.model_validate(
        {"operations": [{"operation": "convert", "params": {"format": "mkv"}}]}
    )
    command = build_edit_command(
        request,
        ffmpeg_binary="ffmpeg",
        source_path=Path("/s"),
        output_path=Path("/o.mkv"),
        preset="veryfast",
        crf=23,
        max_fps=30,
    )

    assert "-vf" not in command and "copy" in command


# --- the worker hands each encoder the job's own choice ----------------------


@asynccontextmanager
async def _sessions():
    yield object()


async def test_the_mp4_job_passes_the_jobs_cap_to_its_step(monkeypatch):
    job = SimpleNamespace(id=uuid4(), source_key="uploads/a/clip.mp4", max_fps=60)
    seen = {}

    async def mark_processing(_session, _job_id):
        return job

    async def mark_done(_session, _job_id, **_fields):
        return job

    class Step:
        def run(self, *, job_id, source_key, max_fps=30):
            seen["max_fps"] = max_fps
            return ProcessingResult(output_key="outputs/a/clip.mp4")

    monkeypatch.setattr(tasks, "mark_processing", mark_processing)
    monkeypatch.setattr(tasks, "mark_done", mark_done)

    await process_job_async(job.id, session_factory=lambda: _sessions(), step=Step())

    assert seen["max_fps"] == 60


async def test_the_ladder_job_passes_the_jobs_cap_to_the_builder(monkeypatch):
    job = SimpleNamespace(
        id=uuid4(),
        status=JobStatus.DONE.value,
        hls_status=HlsStatus.PENDING.value,
        source_key="uploads/a/clip.mp4",
        output_key="outputs/a/clip.mp4",
        operations=None,
        width=3840,
        height=2160,
        duration_seconds=10.0,
        max_fps=60,
    )
    seen = {}

    async def get_job(_session, _job_id):
        return job

    async def mark_ladder_ready(_session, _job_id, *, hls_key):
        return job

    class Ladder:
        def run(self, *, job_id, source_key, probe, max_fps=30):
            seen["max_fps"] = max_fps
            return "outputs/a/hls/master.m3u8"

    monkeypatch.setattr(tasks, "get_job", get_job)
    monkeypatch.setattr(tasks, "mark_ladder_ready", mark_ladder_ready)

    await build_ladder_async(job.id, session_factory=lambda: _sessions(), ladder_step=Ladder())

    assert seen["max_fps"] == 60


# --- the choice survives from upload session to job --------------------------


async def test_a_completed_direct_upload_carries_its_choice_onto_the_job():
    added = []
    session = SimpleNamespace(add=added.append, commit=_noop)
    upload = SimpleNamespace(
        job_id=uuid4(),
        owner_id=uuid4(),
        filename="clip.mp4",
        source_key="uploads/a/clip.mp4",
        max_fps=60,
        state="completing",
    )

    await create_upload_job(session, upload)

    assert added[0].max_fps == 60 and upload.state == "ready"


async def _noop():
    return None
