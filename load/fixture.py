"""Prepare and clean up one real load-test job without saving credentials or signed URLs."""

import argparse
import json
import os
import time
from datetime import UTC, datetime
from pathlib import Path
from uuid import UUID

import httpx
from target import checked_target


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("action", choices=("prepare", "cleanup"))
    parser.add_argument("--video", type=Path)
    parser.add_argument("--evidence", type=Path, required=True)
    args = parser.parse_args()

    base_url = checked_target()
    production = os.environ.get("LOAD_MODE") == "production"
    if production and (
        args.evidence.name != "fixture.json"
        or args.evidence.parent.name != f"cloud-{os.environ['LOAD_RUN_ID']}"
        or args.evidence.parent.resolve().parent != Path("load/results").resolve()
    ):
        parser.error("production evidence must be load/results/cloud-<run_id>/fixture.json")
    if args.action == "prepare":
        if (
            args.video is None
            or not args.video.is_file()
            or args.video.stat().st_size > 1_000_000
        ):
            parser.error("provide a real MP4 at --video with size at most 1,000,000 bytes")
        if production and os.environ.get("LOAD_ALLOW_UPLOAD") != "1":
            parser.error("production upload requires LOAD_ALLOW_UPLOAD=1")
        if args.evidence.exists():
            parser.error("fixture evidence already exists; refusing a second upload")
    email = os.environ["LOAD_EMAIL"]
    password = os.environ["LOAD_PASSWORD"]
    with httpx.Client(base_url=base_url, timeout=30, follow_redirects=False) as client:
        response = client.post("/api/auth/login", json={"email": email, "password": password})
        if response.status_code == 401 and args.action == "prepare" and not production:
            response = client.post(
                "/api/auth/register", json={"email": email, "password": password}
            )
        response.raise_for_status()
        token = response.cookies.get("access_token")
        if not token:
            raise RuntimeError("login did not set access_token")
        client.headers["Cookie"] = f"access_token={token}"

        if args.action == "prepare":
            args.evidence.parent.mkdir(parents=True, exist_ok=True)
            evidence = {
                "captured_at_utc": datetime.now(UTC).isoformat(),
                "run_id": os.environ.get("LOAD_RUN_ID") if production else None,
                "upload_attempted": True,
            }
            with args.evidence.open("x", encoding="utf-8") as record:
                json.dump(evidence, record, indent=2)
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
            UUID(job_id)
            evidence.update(job_id=job_id, video_bytes=args.video.stat().st_size,
                            upload_seconds=round(upload_seconds, 3))
            args.evidence.write_text(json.dumps(evidence, indent=2) + "\n", encoding="utf-8")
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
            evidence.update(terminal_seconds=round(time.monotonic() - started, 3),
                            status=job["status"], has_hls=bool(job.get("hls_url")))
            args.evidence.write_text(json.dumps(evidence, indent=2) + "\n", encoding="utf-8")
            if job["status"] != "done":
                raise RuntimeError(f"job {job_id} failed; see worker logs")
        else:
            evidence = json.loads(args.evidence.read_text(encoding="utf-8"))
            if production and evidence.get("run_id") != os.environ["LOAD_RUN_ID"]:
                raise RuntimeError("fixture belongs to a different production run")
            job_id = evidence["job_id"]
            UUID(job_id)
            if production:
                owned = client.get(f"/api/jobs/{job_id}")
                owned.raise_for_status()
                if owned.json().get("filename") != "load-fixture.mp4":
                    raise RuntimeError("refusing to delete a job outside this fixture")
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