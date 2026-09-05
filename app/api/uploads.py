from typing import Annotated

from fastapi import APIRouter, File, UploadFile

router = APIRouter(tags=["uploads"])

MAX_FILE_SIZE = 100 * 1024 * 1024


@router.post("/upload", status_code=202)
async def upload_video(
    file: Annotated[UploadFile, File(...)],
):
    file.file.seek(0, 2)
    size = file.file.tell()
    file.file.seek(0)

    if size > MAX_FILE_SIZE:
        return {"error": "file too large"}

    return {
        "filename": file.filename,
        "size": size,
    }