from pathlib import Path

from app.services.storage import get_storage_service


async def test_upload_and_download(tmp_path: Path):
    storage = get_storage_service()

    source = tmp_path / "test.txt"
    downloaded = tmp_path / "downloaded-test.txt"

    source.write_text(
        "Hello Flickpond! MinIO storage test.",
        encoding="utf-8",
    )

    object_key = "test/test.txt"

    await storage.upload_file(
        str(source),
        object_key,
    )

    await storage.download_file(
        object_key,
        str(downloaded),
    )

    assert downloaded.exists()

    assert downloaded.read_text(
        encoding="utf-8"
    ) == source.read_text(
        encoding="utf-8"
    )