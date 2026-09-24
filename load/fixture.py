"""Prepare and clean up one real local job without saving credentials or signed URLs."""

import argparse
import json
import os
import time
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import urlsplit

import httpx


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("action", choices=("prepare", "cleanup"))
    parser.add_argument("--video", type=Path)
    parser.add_argument("--evidence", type=Path, required=True)
    args = parser.parse_args()

    base_url = os.environ.get("LOAD_BASE_URL", "")
    target = urlsplit(base_url)
    if (target.scheme, target.hostname, target.port) != ("http", "127.0.0.1", 13000):
        parser.error("only http://127.0.0.1:13000 is permitted")
    email = os.environ["LOAD_EMAIL"]
    password = os.environ["LOAD_PASSWORD"]
    with httpx.Client(base_url=base_url, timeout=30) as client:
        response = client.post("/api/auth/login", json={"email": email, "password": password})
        if response.status_code == 401 and args.action == "prepare":
            response = client.post(
                "/api/auth/register", json={"email": email, "password": password}
            )
        response.raise_for_status()
        token = response.cookies.get("access_token")
        if not token:
            raise RuntimeError("login did not set access_token")
        client.headers["Cookie"] = f"access_token={token}"

        if args.action == "prepare":
            if (
                args.video is None
                or not args.video.is_file()
                or args.video.stat().st_size > 1_000_000
            ):
                parser.error("provide a real MP4 at --video with size at most 1,000,000 bytes")
            started = time.monotonic()
            with args.video.open("rb") as video:
                uploaded = client.post(
                    "/api/upload", files={"file": ("load-fixture.mp4", video, "video/mp4")}
                )
            uploaded.raise_for_status()
            if uploaded.status_code != 202:
                raise RuntimeError(f"expected 202 on upload, got {uploaded.status_code}")
            upload_seconds = time.monotonic() - started
            job_id = uploaded.json()["job_id"]
            deadline = time.monotonic() + 120
            while True:
                job_response = client.get(f"/api/jobs/{job_id}")
                job_response.raise_for_status()
                job = job_response.json()
                if job["status"] in ("done", "failed"):
                    break
                if time.monotonic() >= deadline:
                    raise TimeoutError(f"job {job_id} did not reach a terminal state in 120 s")
                time.sleep(2)
            evidence = {
                "captured_at_utc": datetime.now(UTC).isoformat(),
                "job_id": job_id,
                "video_bytes": args.video.stat().st_size,
                "upload_seconds": round(upload_seconds, 3),
                "terminal_seconds": round(time.monotonic() - started, 3),
                "status": job["status"],
                "has_hls": bool(job.get("hls_url")),
            }
            args.evidence.parent.mkdir(parents=True, exist_ok=True)
            args.evidence.write_text(json.dumps(evidence, indent=2) + "\n", encoding="utf-8")
            if job["status"] != "done":
                raise RuntimeError(f"job {job_id} failed; see worker logs")
        else:
            evidence = json.loads(args.evidence.read_text(encoding="utf-8"))
            job_id = evidence["job_id"]
            deleted = client.delete(f"/api/jobs/{job_id}")
            if deleted.status_code != 204:
                raise RuntimeError(f"delete returned {deleted.status_code}")
            checked = client.get(f"/api/jobs/{job_id}")
            if checked.status_code != 404:
                raise RuntimeError(f"deleted job still returned {checked.status_code}")
            evidence["cleanup_verified"] = True
            args.evidence.write_text(json.dumps(evidence, indent=2) + "\n", encoding="utf-8")
        print(json.dumps(evidence, indent=2))


if __name__ == "__main__":
    main()