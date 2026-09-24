"""Measure the first decoded frame of a completed local job in the real UI."""

import argparse
import json
import os
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import urlsplit

from playwright.sync_api import TimeoutError as PlaywrightTimeoutError
from playwright.sync_api import sync_playwright


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--evidence", type=Path, required=True)
    parser.add_argument("--mp4-only", action="store_true")
    args = parser.parse_args()
    origin = os.environ.get("LOAD_BASE_URL", "")
    target = urlsplit(origin)
    if (target.scheme, target.hostname, target.port) != ("http", "127.0.0.1", 13000):
        parser.error("only http://127.0.0.1:13000 is permitted")
    fixture = json.loads(args.evidence.read_text(encoding="utf-8"))
    job_id = fixture["job_id"]
    network = {"hls_redirects": 0, "object_successes": 0, "object_failures": 0}
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(channel="msedge", headless=True)
        page = browser.new_page(viewport={"width": 1280, "height": 800})

        def record_response(response):
            path = urlsplit(response.url).path
            if f"/api/jobs/{job_id}/hls/" in path and response.status == 307:
                network["hls_redirects"] += 1
            if path.startswith("/videos/"):
                key = "object_successes" if response.status in (200, 206) else "object_failures"
                network[key] += 1

        page.on("response", record_response)
        page.goto(origin, wait_until="domcontentloaded")
        page.locator("#auth-email").fill(os.environ["LOAD_EMAIL"])
        page.locator("#auth-password").fill(os.environ["LOAD_PASSWORD"])
        page.locator("#auth-submit").click()
        page.locator("#nav-library").wait_for(state="visible", timeout=10000)
        page.locator("#nav-library").click()
        card = page.locator(".video-card").filter(has_text="load-fixture.mp4")
        card.locator(".video-thumb.playable").wait_for(state="visible", timeout=10000)
        if args.mp4_only:
            page.evaluate("window.Hls = undefined")
        page.evaluate(
            """() => {
              window.__firstFrameAt = null;
              const observer = new MutationObserver(() => {
                const video = document.querySelector('.video-card .player-host video');
                if (!video) return;
                observer.disconnect();
                video.requestVideoFrameCallback(() => {
                  window.__firstFrameAt = performance.now();
                });
              });
                            observer.observe(document.querySelector('#library-grid'), {
                                childList: true, subtree: true
                            });
            }"""
        )
        started = page.evaluate("performance.now()")
        card.locator(".video-thumb.playable").click()
        first_frame_ms = None
        try:
            page.wait_for_function("window.__firstFrameAt !== null", timeout=15000)
            first_frame_ms = round(page.evaluate("window.__firstFrameAt") - started, 1)
        except PlaywrightTimeoutError:
            pass
        finally:
            media_error = page.evaluate(
                """() => document.querySelector('.video-card .player-host video')
                  ?.error?.code ?? null"""
            )
            browser.close()
    result = {
        "captured_at_utc": datetime.now(UTC).isoformat(),
        "job_id": job_id,
        "mode": "mp4-only" if args.mp4_only else "hls",
        "first_frame_ms": first_frame_ms,
        "media_error": media_error,
        "network": network,
    }
    output = args.evidence.with_name("playback-mp4.json" if args.mp4_only else "playback-hls.json")
    output.write_text(json.dumps(result, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(result, indent=2))
    if first_frame_ms is None or network["object_successes"] == 0:
        raise RuntimeError("no video frame was decoded; inspect saved media and network evidence")


if __name__ == "__main__":
    main()