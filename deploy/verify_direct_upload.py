"""Upload a real video, verify browser CORS/ETag and resume after the first part.

Credentials are read from UPLOAD_TEST_EMAIL / UPLOAD_TEST_PASSWORD; evidence
contains no credentials, cookies, presigned URLs or storage identifiers.
"""

import argparse
import hashlib
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


class UploadPaused(Exception):
    """An intentional stop with a durable session available to the next process."""


def file_identity(video):
    with video.open("rb") as source:
        digest = hashlib.file_digest(source, "sha256").hexdigest()
    return {"filename": video.name, "video_bytes": video.stat().st_size, "sha256": digest}


def read_resume_evidence(path, identity, api_base, origin):
    evidence = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(evidence, dict):
        raise ValueError("resume evidence must be an object")
    expected = {**identity, "api_base": api_base, "origin": origin}
    if any(evidence.get(key) != value for key, value in expected.items()):
        raise ValueError("resume requires the same video bytes, API target and browser origin")
    if not evidence.get("upload_id"):
        raise ValueError("evidence has no upload session; start a new verification")
    return evidence


def upload_and_resume(
    api, storage, video, origin, *, resumed=None, upload_id=None,
    checkpoint=None, stop_after_first_part=False,
):
    def record(**fields):
        if checkpoint:
            checkpoint(**fields)

    if upload_id:
        response = api.get(f"uploads/{upload_id}")
        response.raise_for_status()
        session = response.json()
        uid = upload_id
        manifest = session["parts_done"]
        state = session["state"]
        if state == "completed":
            return session["job_id"], uid, session["part_count"]
        if state not in {"open", "completing", "ready"}:
            raise RuntimeError("upload is cancelled; start a new verification")
        record(resumed_parts=len(manifest))
    else:
        started = api.post("uploads", json={
            "filename": video.name, "size": video.stat().st_size, "content_type": "",
        })
        started.raise_for_status()
        session = started.json()
        uid = session["upload_id"]
        manifest = []
        state = "open"
        # Persist before the first PUT, so process termination never loses the ID.
        record(upload_id=uid, part_count=session["part_count"])
    if state == "open":
        done = {part["n"] for part in manifest}
        checked_cors = False
        with video.open("rb") as source:
            for number in range(1, session["part_count"] + 1):
                if number in done:
                    continue
                source.seek((number - 1) * session["part_size"])
                signed = api.post(f"uploads/{uid}/parts", json={"part_numbers": [number]})
                signed.raise_for_status()
                url = signed.json()["urls"][str(number)]
                if not checked_cors:
                    preflight = storage.options(url, headers={
                        "Origin": origin, "Access-Control-Request-Method": "PUT",
                    })
                    cors(preflight, origin)
                    methods = preflight.headers.get("access-control-allow-methods", "")
                    if "PUT" not in methods.upper():
                        raise RuntimeError("storage CORS does not allow PUT")
                    checked_cors = True
                response = storage.put(
                    url, content=source.read(session["part_size"]), headers={"Origin": origin}
                )
                cors(response, origin)
                exposed = response.headers.get("access-control-expose-headers", "")
                if "etag" not in {h.strip().lower() for h in exposed.split(",")}:
                    raise RuntimeError("storage CORS must explicitly expose ETag")
                manifest.append({"n": number, "etag": response.headers["etag"]})
                record(cors_etag_verified=True)
                if number == 1:
                    progress = api.get(f"uploads/{uid}")
                    progress.raise_for_status()
                    if progress.json()["parts_done"] != manifest:
                        raise RuntimeError("resume did not recover the first part and its ETag")
                    manifest = progress.json()["parts_done"]
                    record(resumed_after_part=1)
                    if resumed:
                        resumed(uid)
                    if stop_after_first_part:
                        raise UploadPaused
    # A lost completion response must retry the frozen manifest, never PUT again.
    completed = api.post(f"uploads/{uid}/complete", json={
        "parts": sorted(manifest, key=lambda part: part["n"]),
    })
    completed.raise_for_status()
    return completed.json()["job_id"], uid, session["part_count"]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--api-base", required=True, help="e.g. https://staging.example/api/")
    parser.add_argument("--origin", required=True, help="exact browser origin, without a path")
    parser.add_argument("--video", type=Path, required=True)
    parser.add_argument("--evidence", type=Path, required=True)
    parser.add_argument("--minimum-bytes", type=int, default=1_000_000_000)
    parser.add_argument("--wait-seconds", type=int, default=1800)
    parser.add_argument("--resume", action="store_true", help="reuse the evidence upload session")
    parser.add_argument("--stop-after-first-part", action="store_true",
                        help="save a session and exit before uploading the remaining parts")
    args = parser.parse_args()
    if not args.video.is_file() or args.video.stat().st_size < args.minimum_bytes:
        parser.error("provide a real video at least --minimum-bytes in size")
    if args.evidence.exists() and not args.resume:
        parser.error("evidence already exists; use --resume or a new path")
    if args.video.stat().st_size <= 16 * 1024 * 1024:
        parser.error("resume verification needs a video larger than one part")
    identity = file_identity(args.video)
    if args.resume:
        try:
            evidence = read_resume_evidence(
                args.evidence, identity, args.api_base, args.origin
            )
        except (OSError, ValueError) as exc:
            parser.error(str(exc))
    else:
        evidence = {
            "captured_at_utc": datetime.now(UTC).isoformat(), "api_base": args.api_base,
            "origin": args.origin, **identity,
        }
        args.evidence.parent.mkdir(parents=True, exist_ok=True)
        with args.evidence.open("x", encoding="utf-8") as record:
            json.dump(evidence, record, indent=2)
    started = time.monotonic()

    def save():
        temporary = args.evidence.with_name(args.evidence.name + ".tmp")
        temporary.write_text(json.dumps(evidence, indent=2) + "\n", encoding="utf-8")
        temporary.replace(args.evidence)

    def checkpoint(**fields):
        evidence.update(fields)
        save()

    try:
        with httpx.Client(base_url=args.api_base.rstrip("/") + "/", timeout=120) as api:
            login = api.post("auth/login", json={
                "email": os.environ["UPLOAD_TEST_EMAIL"],
                "password": os.environ["UPLOAD_TEST_PASSWORD"],
            })
            login.raise_for_status()
            owner_id = login.json()["id"]
            if args.resume and evidence.get("owner_id") != owner_id:
                raise ValueError("resume requires the account that started this upload")
            checkpoint(owner_id=owner_id)
            # Isolate storage requests so signed URLs never receive auth cookies.
            with httpx.Client(timeout=120) as storage:
                job_id, uid, count = upload_and_resume(
                    api, storage, args.video, args.origin,
                    upload_id=evidence.get("upload_id"), checkpoint=checkpoint,
                    stop_after_first_part=args.stop_after_first_part,
                )
            evidence.update(
                job_id=job_id, upload_id=uid, part_count=count,
                upload_seconds=round(time.monotonic() - started, 3),
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
    except UploadPaused:
        checkpoint(status="paused")
        print("Paused after the first part. Re-run the same command with --resume.")
        return
    except (Exception, KeyboardInterrupt) as exc:
        # Exception text from HTTP clients can contain signed URLs or cookies.
        evidence["failure_type"] = type(exc).__name__
        save()
        raise SystemExit(
            f"verification failed ({type(exc).__name__}); inspect private logs"
        ) from None
    evidence.pop("failure_type", None)
    evidence["elapsed_seconds"] = round(time.monotonic() - started, 3)
    save()
    print(json.dumps(evidence, indent=2))


if __name__ == "__main__":
    main()
