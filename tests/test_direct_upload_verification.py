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


@pytest.mark.parametrize("interrupted", [False, True])
def test_a_second_run_reuses_the_session_and_only_sends_missing_bytes(tmp_path, interrupted):
    from deploy.verify_direct_upload import UploadPaused

    video = tmp_path / "resume.mkv"
    video.write_bytes(b"abcdef")
    evidence = {}
    parts = []
    signed_numbers = []
    calls = []
    transferred = []

    def api_handler(request):
        calls.append((request.method, request.url.path))
        if request.url.path == "/api/uploads":
            return httpx.Response(201, json={"upload_id": "u", "part_size": 4, "part_count": 2})
        if request.method == "GET":
            return httpx.Response(200, json={
                "parts_done": list(parts), "state": "open", "part_size": 4, "part_count": 2,
            })
        if request.url.path.endswith("/parts"):
            number = json.loads(request.content)["part_numbers"][0]
            signed_numbers.append(number)
            return httpx.Response(200, json={"urls": {str(number): f"https://storage.test/{number}"}})
        assert request.url.path.endswith("/complete")
        assert json.loads(request.content)["parts"] == parts
        return httpx.Response(202, json={"job_id": "j"})

    def storage_handler(request):
        headers = {"Access-Control-Allow-Origin": "https://staging.test",
                   "Access-Control-Allow-Methods": "PUT", "Access-Control-Expose-Headers": "ETag"}
        if request.method == "PUT":
            number = int(request.url.path[1:])
            if number == 2 and interrupted and len(signed_numbers) == 2:
                raise httpx.ReadError("transfer interrupted")
            assert "upload_id" in evidence
            transferred.append(request.content)
            parts.append({"n": number, "etag": f'"e{number}"'})
            headers["ETag"] = f'"e{number}"'
        return httpx.Response(200, headers=headers)

    def invoke(**kwargs):
        # Each invocation uses fresh clients, as separate CLI processes would.
        with httpx.Client(base_url="https://app.test/api/",
                          transport=httpx.MockTransport(api_handler)) as api:
            with httpx.Client(transport=httpx.MockTransport(storage_handler)) as storage:
                return upload_and_resume(
                    api, storage, video, "https://staging.test",
                    checkpoint=lambda **fields: evidence.update(fields), **kwargs,
                )

    with pytest.raises(httpx.ReadError if interrupted else UploadPaused):
        invoke(stop_after_first_part=not interrupted)
    assert parts == [{"n": 1, "etag": '"e1"'}]
    assert not any(method == "DELETE" for method, _ in calls)
    assert invoke(upload_id=evidence["upload_id"]) == ("j", "u", 2)
    assert calls.count(("POST", "/api/uploads")) == 1
    assert signed_numbers == ([1, 2, 2] if interrupted else [1, 2])
    assert transferred == [b"abcd", b"ef"]
    assert evidence["resumed_parts"] == 1


@pytest.mark.parametrize("state", ["completing", "ready", "completed"])
def test_resume_after_completion_started_never_uploads_or_creates_again(tmp_path, state):
    video = tmp_path / "resume.mkv"
    video.write_bytes(b"abcdef")
    calls = []
    manifest = [{"n": 1, "etag": '"e1"'}, {"n": 2, "etag": '"e2"'}]

    def api_handler(request):
        calls.append(request.method)
        if request.method == "GET":
            return httpx.Response(200, json={
                "state": state, "parts_done": manifest, "part_count": 2, "job_id": "j",
            })
        assert request.url.path == "/api/uploads/u/complete"
        assert json.loads(request.content)["parts"] == manifest
        return httpx.Response(202, json={"job_id": "j"})

    def unexpected_storage(request):
        pytest.fail("a completion retry must not send another storage request")

    with httpx.Client(base_url="https://app.test/api/",
                      transport=httpx.MockTransport(api_handler)) as api:
        with httpx.Client(transport=httpx.MockTransport(unexpected_storage)) as storage:
            assert upload_and_resume(
                api, storage, video, "https://staging.test", upload_id="u"
            ) == ("j", "u", 2)
    assert calls == (["GET"] if state == "completed" else ["GET", "POST"])


@pytest.mark.parametrize("change", ["same_size_bytes", "api_base", "origin", "missing_session"])
def test_resume_evidence_rejects_a_different_file_or_target(tmp_path, change):
    from deploy.verify_direct_upload import file_identity, read_resume_evidence

    video = tmp_path / "resume.mkv"
    video.write_bytes(b"abcdef")
    api_base, origin = "https://app.test/api/", "https://staging.test"
    evidence = {**file_identity(video), "api_base": api_base, "origin": origin, "upload_id": "u"}
    if change == "same_size_bytes":
        video.write_bytes(b"abcdeg")
    elif change == "missing_session":
        evidence.pop("upload_id")
    else:
        evidence[change] = "https://other.test"
    path = tmp_path / "evidence.json"
    path.write_text(json.dumps(evidence), encoding="utf-8")
    with pytest.raises(ValueError):
        read_resume_evidence(path, file_identity(video), api_base, origin)
