"""Real presigned PUT, list/resume, range read, completion and abort against MinIO/S3."""

import os
from types import SimpleNamespace
from uuid import uuid4

import httpx
import pytest

from app.services.multipart_storage import get_multipart_storage

pytestmark = pytest.mark.skipif(
    os.getenv("RUN_POSTGRES_TESTS") != "1",
    reason="set RUN_POSTGRES_TESTS=1 to run tests that need the live compose stack",
)


async def test_direct_put_can_resume_complete_and_range_read():
    store = get_multipart_storage()
    key = f"uploads/integration-{uuid4()}/test.mp4"
    uid = await store.initiate(key)
    upload = SimpleNamespace(source_key=key, storage_id=uid)
    payload = b"\x00\x00\x00\x20ftypisom" + b"\x00" * 4096
    try:
        urls = await store.sign(upload, [1], 120)
        async with httpx.AsyncClient() as client:
            response = await client.put(urls["1"], content=payload)
            response.raise_for_status()
        etag = response.headers["etag"]
        assert await store.parts(upload) == [{"n": 1, "etag": etag, "size": len(payload)}]
        upload.manifest = [{"n": 1, "etag": etag}]
        await store.complete(upload)
        head = await store.head(key)
        assert head["ContentLength"] == len(payload)
        assert head["ContentType"] == "application/octet-stream"
        assert head["ContentDisposition"] == "attachment"
        assert await store.sniff(key) == payload[:4096]
    finally:
        await store.abort(upload)
    assert await store.head(key) is None


async def test_abort_removes_parts_and_is_repeatable():
    store = get_multipart_storage()
    key = f"uploads/integration-{uuid4()}/cancel.mp4"
    uid = await store.initiate(key)
    upload = SimpleNamespace(source_key=key, storage_id=uid)
    try:
        urls = await store.sign(upload, [1], 120)
        async with httpx.AsyncClient() as client:
            response = await client.put(urls["1"], content=b"partial upload")
            response.raise_for_status()
        assert len(await store.parts(upload)) == 1
    finally:
        await store.abort(upload)
    await store.abort(upload)
    assert await store.head(key) is None
