"""Upload a real video, verify browser CORS/ETag and resume after the first part.

Credentials are read from UPLOAD_TEST_EMAIL / UPLOAD_TEST_PASSWORD; evidence
contains no credentials, cookies, presigned URLs or storage identifiers.
"""

import argparse
import json
import os
import time
from datetime import UTC, datetime
from pathlib import Path

import httpx


def cors(response, origin):
    response.raise_for_status()
    if response.headers.get("access-control-allow-origin") not in {origin, "*"}:
        raise RuntimeError("storage does not allow the browser origin")


def upload_and_resume(api, storage, video, origin, *, resumed=None):
    started = api.post("uploads", json={
        "filename": video.name, "size": video.stat().st_size, "content_type": "",
    })
    started.raise_for_status()
    session = started.json()
    uid = session["upload_id"]
    manifest = []
    try:
        with video.open("rb") as source:
            for number in range(1, session["part_count"] + 1):
                signed = api.post(f"uploads/{uid}/parts", json={"part_numbers": [number]})
                signed.raise_for_status()
                url = signed.json()["urls"][str(number)]
                if number == 1:
                    preflight = storage.options(url, headers={
                        "Origin": origin, "Access-Control-Request-Method": "PUT",
                    })
                    cors(preflight, origin)
                    methods = preflight.headers.get("access-control-allow-methods", "")
                    if "PUT" not in methods.upper():
                        raise RuntimeError("storage CORS does not allow PUT")
                response = storage.put(
                    url, content=source.read(session["part_size"]), headers={"Origin": origin}
                )
                cors(response, origin)
                exposed = response.headers.get("access-control-expose-headers", "")
                if "etag" not in {h.strip().lower() for h in exposed.split(",")}:
                    raise RuntimeError("storage CORS must explicitly expose ETag")
                manifest.append({"n": number, "etag": response.headers["etag"]})
                if number == 1:
                    # Drop all transfer state and reconstruct it from the API, as
                    # a reloaded browser does. The first part must not be sent twice.
                    progress = api.get(f"uploads/{uid}")
                    progress.raise_for_status()
                    if progress.json()["parts_done"] != manifest:
                        raise RuntimeError("resume did not recover the first part and its ETag")
                    manifest = progress.json()["parts_done"]
                    if resumed:
                        resumed(uid)
        completed = api.post(f"uploads/{uid}/complete", json={"parts": manifest})
        completed.raise_for_status()
        return completed.json()["job_id"], uid, session["part_count"]
    except Exception:
        # Best effort only: completing sessions need completion retried, not abort.
        api.delete(f"uploads/{uid}")
        raise


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--api-base", required=True, help="e.g. https://staging.example/api/")
    parser.add_argument("--origin", required=True, help="exact browser origin, without a path")
    parser.add_argument("--video", type=Path, required=True)
    parser.add_argument("--evidence", type=Path, required=True)
    parser.add_argument("--minimum-bytes", type=int, default=1_000_000_000)
    parser.add_argument("--wait-seconds", type=int, default=1800)
    args = parser.parse_args()
    if not args.video.is_file() or args.video.stat().st_size < args.minimum_bytes:
        parser.error("provide a real video at least --minimum-bytes in size")
    if args.evidence.exists():
        parser.error("evidence already exists; use a new path")
    if args.video.stat().st_size <= 16 * 1024 * 1024:
        parser.error("resume verification needs a video larger than one part")
    evidence = {
        "captured_at_utc": datetime.now(UTC).isoformat(), "api_base": args.api_base,
        "origin": args.origin, "video_bytes": args.video.stat().st_size,
    }
    args.evidence.parent.mkdir(parents=True, exist_ok=True)
    with args.evidence.open("x", encoding="utf-8") as record:
        json.dump(evidence, record, indent=2)
    started = time.monotonic()

    def save():
        args.evidence.write_text(json.dumps(evidence, indent=2) + "\n", encoding="utf-8")

    try:
        with httpx.Client(base_url=args.api_base.rstrip("/") + "/", timeout=120) as api:
            login = api.post("auth/login", json={
                "email": os.environ["UPLOAD_TEST_EMAIL"],
                "password": os.environ["UPLOAD_TEST_PASSWORD"],
            })
            login.raise_for_status()
            # Isolate storage requests so signed URLs never receive auth cookies.
            with httpx.Client(timeout=120) as storage:
                job_id, uid, count = upload_and_resume(
                    api, storage, args.video, args.origin,
                    resumed=lambda upload_id: evidence.update(
                        upload_id=upload_id, resumed_after_part=1
                    ),
                )
            evidence.update(
                job_id=job_id, upload_id=uid, part_count=count,
                cors_etag_verified=True, upload_seconds=round(time.monotonic() - started, 3),
            )
            save()
            deadline = time.monotonic() + args.wait_seconds
            while time.monotonic() < deadline:
                response = api.get(f"jobs/{job_id}")
                response.raise_for_status()
                job = response.json()
                if job["status"] == "failed":
                    raise RuntimeError("processing failed; inspect worker logs")
                if job["status"] == "done":
                    evidence.update(status="done", has_hls=bool(job.get("hls_url")))
                    break
                time.sleep(2)
            else:
                raise TimeoutError("processing did not finish before the deadline")
    except Exception as exc:
        # Exception text from HTTP clients can contain signed URLs or cookies.
        evidence["failure_type"] = type(exc).__name__
        save()
        raise SystemExit(
            f"verification failed ({type(exc).__name__}); inspect private logs"
        ) from None
    evidence["elapsed_seconds"] = round(time.monotonic() - started, 3)
    save()
    print(json.dumps(evidence, indent=2))


if __name__ == "__main__":
    main()
