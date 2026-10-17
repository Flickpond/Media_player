"""Safe upload names and the retired one-shot endpoint."""

import re
from pathlib import PurePosixPath
from typing import Annotated

from fastapi import APIRouter, Depends
from fastapi.responses import JSONResponse

from app.api.deps import CurrentUser
from app.services.storage import StorageService, get_storage_service

router = APIRouter(tags=["uploads"])
# The global request-body backstop; video bytes now travel directly to storage.
MAX_FILE_SIZE = 100 * 1024 * 1024
DEFAULT_FILENAME = "upload.bin"
MAX_FILENAME_LENGTH = 100
_UNSAFE_FILENAME_CHARS = re.compile(r"[^A-Za-z0-9._-]")
StorageDependency = Annotated[StorageService, Depends(get_storage_service)]


def safe_filename(raw: str | None) -> str:
    """Keep object keys under the reserved job prefix, regardless of client input."""
    name = PurePosixPath(raw or "").name
    name = _UNSAFE_FILENAME_CHARS.sub("_", name).lstrip(".")
    return name[:MAX_FILENAME_LENGTH] or DEFAULT_FILENAME


@router.post("/upload", status_code=410)
async def upload_video(_user: CurrentUser):
    # No File dependency: a retired request must never spool video bytes to disk.
    return JSONResponse(
        {"error": "POST /upload is retired; use POST /uploads for resumable direct uploads"},
        status_code=410,
    )
