"""The site in a real browser: sign up, upload, play, edit.

Three sprint 5 bugs passed every jsdom test and showed only in Chromium --
Chrome's own HLS player failing (BUG-20), a crop box that could not draw
(BUG-21), a player rebuilt every poll (BUG-25). This drives the running stack
the way a person does and checks what they would see.

Skipped unless RUN_BROWSER_TESTS=1, against a stack already up at
SMOKE_BASE_URL (default http://localhost:3000): `docker compose up -d`.
"""

import os
import time
from pathlib import Path
from uuid import uuid4

import pytest

pytestmark = pytest.mark.skipif(
    os.getenv("RUN_BROWSER_TESTS") != "1",
    reason="set RUN_BROWSER_TESTS=1, with the stack running, to drive a real browser",
)

BASE_URL = os.getenv("SMOKE_BASE_URL", "http://localhost:3000")
VIDEO = Path(__file__).with_name("smoke.mp4")  # 3 s, 640x360, with sound
PROCESSING_TIMEOUT_MS = 240_000

# Expected noise: the page asks who is signed in before anyone is, and a
# player handed over to hls.js releases its first source.
EXPECTED_CONSOLE = ("401", "ERR_FILE_NOT_FOUND")


@pytest.fixture
def page():
    from playwright.sync_api import sync_playwright

    with sync_playwright() as playwright:
        browser = playwright.chromium.launch()
        context = browser.new_context(viewport={"width": 1280, "height": 900})
        page = context.new_page()
        page.console_errors = []
        page.on(
            "console",
            lambda message: page.console_errors.append(message.text)
            if message.type == "error"
            else None,
        )
        yield page
        browser.close()


def sign_up(page) -> str:
    page.goto(BASE_URL)
    email = f"smoke-{uuid4().hex[:12]}@example.com"
    statuses = page.evaluate(
        """async (email) => {
            const body = JSON.stringify({ email, password: "smoke-test-password-1" });
            const headers = { "Content-Type": "application/json" };
            const made = await fetch("/api/auth/register", { method: "POST", headers, body });
            const signed = await fetch("/api/auth/login", { method: "POST", headers, body });
            return [made.status, signed.status];
        }""",
        email,
    )
    assert statuses == [201, 200]
    page.reload()
    page.wait_for_selector("#app:not([hidden])")
    return email


def test_a_person_can_sign_up_upload_watch_and_crop(page):
    sign_up(page)

    # Upload and wait for the job to finish, as the upload card reports it.
    page.set_input_files("#file-input", str(VIDEO))
    page.wait_for_function(
        "document.getElementById('status').textContent === 'Processing complete.'",
        timeout=PROCESSING_TIMEOUT_MS,
    )

    # It plays: a decoded frame, no media error, and through hls.js where
    # MediaSource exists -- the player Chrome's own HLS broke (BUG-20).
    page.wait_for_function(
        "(() => { const v = document.querySelector('#player video');"
        " return v && v.readyState >= 2; })()",
        timeout=30_000,
    )
    playing = page.evaluate(
        "(() => { const v = document.querySelector('#player video');"
        " return { error: v.error && v.error.code, src: v.currentSrc }; })()"
    )
    assert playing["error"] is None
    assert playing["src"].startswith("blob:"), "the ladder plays through hls.js"

    # The player is built once, not every poll (BUG-25).
    first = page.evaluate("document.querySelector('#player video') !== null")
    page.evaluate("window.__smokePlayer = document.querySelector('#player video')")
    time.sleep(5)
    assert first and page.evaluate(
        "window.__smokePlayer === document.querySelector('#player video')"
    )

    # The library lists it, and the editor opens on it.
    page.click("#nav-library")
    page.wait_for_selector("#library-grid .card-edit")
    page.click("#library-grid .card-edit")
    page.wait_for_selector("#view-edit:not([hidden])")

    # A crop box drawn with the mouse reports a box in the video's pixels
    # (BUG-21: the first press used to move a box that filled the frame).
    page.click("details.edit-op:first-of-type summary")
    page.check("#edit-crop")
    page.wait_for_selector(".crop-box:not(.is-disabled)", timeout=30_000)
    stage = page.locator(".crop-stage").bounding_box()
    page.mouse.move(stage["x"] + stage["width"] * 0.25, stage["y"] + stage["height"] * 0.25)
    page.mouse.down()
    page.mouse.move(
        stage["x"] + stage["width"] * 0.75, stage["y"] + stage["height"] * 0.75, steps=8
    )
    page.mouse.up()
    readout = page.locator(".crop-readout").text_content()
    assert "of 640 × 360" in readout and not readout.startswith("640 × 360"), readout
    assert page.locator("#edit-process").is_enabled()

    unexpected = [
        text for text in page.console_errors if not any(e in text for e in EXPECTED_CONSOLE)
    ]
    assert unexpected == []
