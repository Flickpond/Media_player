import json

import httpx
import pytest

from deploy.verify_direct_upload import upload_and_resume


def test_verifier_preserves_api_prefix_resumes_parts_and_keeps_cookies_off_storage(tmp_path):
    video = tmp_path / "test.mkv"
    video.write_bytes(b"abcdef")
    manifest = []
    puts = []

    def api_handler(request):
        assert request.url.path.startswith("/api/uploads")
        if request.url.path == "/api/uploads":
            assert json.loads(request.content)["content_type"] == ""
            return httpx.Response(201, json={"upload_id": "u", "part_size": 4, "part_count": 2})
        if request.url.path.endswith("/parts"):
            n = json.loads(request.content)["part_numbers"][0]
            return httpx.Response(200, json={"urls": {str(n): f"https://storage.test/{n}"}})
        if request.method == "GET":
            assert puts == [b"abcd"]
            return httpx.Response(200, json={"parts_done": [{"n": 1, "etag": '"e1"'}]})
        assert request.url.path.endswith("/complete")
        manifest.extend(json.loads(request.content)["parts"])
        return httpx.Response(202, json={"job_id": "j"})

    def storage_handler(request):
        assert "cookie" not in request.headers
        assert request.headers["origin"] == "https://staging.test"
        headers = {"Access-Control-Allow-Origin": "https://staging.test"}
        if request.method == "OPTIONS":
            headers["Access-Control-Allow-Methods"] = "PUT"
        else:
            puts.append(request.content)
            headers.update({"ETag": f'"e{len(puts)}"', "Access-Control-Expose-Headers": "ETag"})
        return httpx.Response(200, headers=headers)

    with httpx.Client(base_url="https://app.test/api/", cookies={"auth": "private"},
                      transport=httpx.MockTransport(api_handler)) as api:
        with httpx.Client(transport=httpx.MockTransport(storage_handler)) as storage:
            assert upload_and_resume(api, storage, video, "https://staging.test") == ("j", "u", 2)
    assert puts == [b"abcd", b"ef"]
    assert manifest == [{"n": 1, "etag": '"e1"'}, {"n": 2, "etag": '"e2"'}]


def test_verifier_rejects_a_cors_response_without_the_browser_origin(tmp_path):
    from deploy.verify_direct_upload import cors

    response = httpx.Response(200, request=httpx.Request("PUT", "https://storage.test/part"))
    with pytest.raises(RuntimeError, match="browser origin"):
        cors(response, "https://staging.test")
