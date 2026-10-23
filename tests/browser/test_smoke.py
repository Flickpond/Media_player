"""The site in a real browser: sign up, upload, play, edit.

Three sprint 5 bugs passed every jsdom test and showed only in Chromium --
Chrome's own HLS player failing (BUG-20), a crop box that could not draw
(BUG-21), a player rebuilt every poll (BUG-25). This drives the running stack
the way a person does and checks what they would see.

Skipped unless RUN_BROWSER_TESTS=1, against a stack already up at
SMOKE_BASE_URL (default http://localhost:3000): `docker compose up -d`.
"""

import base64
import json
import math
import os
import shutil
import subprocess
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
# A storage origin that does not allow reading the frame into a WebGL texture
# is handled by the CSS fallback, but the browser may still log the refused
# texture upload. That is the expected path, not a failure of the page.
EXPECTED_PRESET_CONSOLE = ("SecurityError", "texImage2D", "CORS", "cross-origin")


def _media_command(tool: str) -> list[str]:
    return [tool] if shutil.which(tool) else ["docker", "compose", "exec", "-T", "worker", tool]


def _ffmpeg(pixels: bytes, inputs: list[str], outputs: list[str]) -> bytes:
    return subprocess.run(
        [
            *_media_command("ffmpeg"),
            "-hide_banner",
            "-loglevel",
            "error",
            *inputs,
            "-i",
            "pipe:0",
            *outputs,
            "pipe:1",
        ],
        input=pixels,
        capture_output=True,
        check=True,
        timeout=60,
    ).stdout


@pytest.fixture(scope="module", params=["gradient", "bt709", "video"])
def colour_frame(request):
    """A full colour ramp and a decoded frame, independent of the preview LUT."""
    width, height = 640, 360
    if request.param in ("gradient", "bt709"):
        rgb = bytes(
            value
            for y in range(height)
            for x in range(width)
            for value in (
                round(x * 255 / (width - 1)),
                round(y * 255 / (height - 1)),
                round((1 + math.sin((x + y) / 100)) * 127.5),
            )
        )
    else:
        rgb = _ffmpeg(
            VIDEO.read_bytes(), [], ["-frames:v", "1", "-f", "rawvideo", "-pix_fmt", "rgb24"]
        )
    inputs = ["-f", "rawvideo", "-pix_fmt", "rgb24", "-s", f"{width}x{height}"]
    if request.param == "bt709":
        rgb = _ffmpeg(
            rgb,
            inputs,
            [
                "-vf",
                "scale=out_color_matrix=bt709:out_range=tv,format=yuv420p",
                "-frames:v",
                "1",
                "-f",
                "rawvideo",
                "-pix_fmt",
                "yuv420p",
            ],
        )
        inputs = [
            "-f",
            "rawvideo",
            "-pix_fmt",
            "yuv420p",
            "-s",
            f"{width}x{height}",
            "-colorspace",
            "bt709",
        ]
    png = _ffmpeg(rgb, inputs, ["-frames:v", "1", "-f", "image2pipe", "-c:v", "png"])
    return request.param, rgb, png, inputs, width, height


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
            lambda message: (
                page.console_errors.append(message.text) if message.type == "error" else None
            ),
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


@pytest.mark.parametrize("preset", ["bw", "warm", "cool", "vintage", "vivid"])
def test_the_webgl_preview_matches_real_ffmpeg(page, colour_frame, preset):
    name, rgb, png, inputs, width, height = colour_frame
    manifest = json.loads(
        (Path(__file__).resolve().parents[2] / "frontend" / "preset-luts.json").read_text(
            encoding="utf-8"
        )
    )
    reference = _ffmpeg(
        rgb,
        inputs,
        [
            "-vf",
            ",".join(["format=yuv420p", *manifest["filters"][preset], "format=rgb24"]),
            "-frames:v",
            "1",
            "-f",
            "rawvideo",
            "-pix_fmt",
            "rgb24",
        ],
    )
    page.goto(BASE_URL)
    page.evaluate(
        """async encoded => {
            document.body.replaceChildren();
            // Capture immediately after the real GPU draw: WebGL may clear
            // its default framebuffer after presenting it to the compositor.
            const getContext = HTMLCanvasElement.prototype.getContext;
            HTMLCanvasElement.prototype.getContext = function(...args) {
                const gl = getContext.apply(this, args);
                if (args[0] === 'webgl' && gl) {
                    const draw = gl.drawArrays.bind(gl);
                    gl.drawArrays = (...arguments_) => {
                        draw(...arguments_);
                        const rgba = new Uint8Array(this.width * this.height * 4);
                        gl.readPixels(0, 0, this.width, this.height,
                                      gl.RGBA, gl.UNSIGNED_BYTE, rgba);
                        window.previewPixels = rgba;
                    };
                }
                return gl;
            };
            const stage = document.createElement('div');
            stage.style.cssText = 'position:relative;width:640px;height:360px';
            const image = new Image();
            image.src = 'data:image/png;base64,' + encoded;
            await image.decode();
            Object.defineProperties(image, {
                videoWidth: {value: image.naturalWidth},
                videoHeight: {value: image.naturalHeight},
                readyState: {value: 4}, paused: {value: true}, ended: {value: false}
            });
            stage.append(image);
            const controls = document.createElement('div');
            document.body.append(stage, controls);
            window.mountPresetPanel(image, () => {}, {host: controls});
        }""",
        base64.b64encode(png).decode(),
    )
    page.click(f'.preset-tile[data-preset="{preset}"]')
    page.wait_for_function("window.previewPixels?.length > 0", timeout=15_000)
    metrics = page.evaluate(
        """({encoded, width, height}) => {
            const expected = atob(encoded);
            const actual = window.previewPixels;
            let squared = 0, absolute = 0;
            for (let y = 0; y < height; y++) {
                for (let x = 0; x < width; x++) {
                    for (let c = 0; c < 3; c++) {
                        const i = (y * width + x) * 3 + c;
                        const j = ((height - y - 1) * width + x) * 4 + c;
                        const error = actual[j] - expected.charCodeAt(i);
                        squared += error * error;
                        absolute += Math.abs(error);
                    }
                }
            }
            return {mae: absolute / expected.length,
                    psnr: 10 * Math.log10(255 * 255 / (squared / expected.length))};
        }""",
        {"encoded": base64.b64encode(reference).decode(), "width": width, "height": height},
    )
    print(f"{name}/{preset}: MAE={metrics['mae']:.2f}/255, PSNR={metrics['psnr']:.2f} dB")
    assert metrics["mae"] < 4, metrics
    assert metrics["psnr"] > 30, metrics


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

    # Fullscreen fills the screen: the 360px cap that keeps tall sources out
    # of the cards used to hold the video to a strip at the top.
    page.click("#player [data-plyr='fullscreen']", force=True)
    page.wait_for_function("document.fullscreenElement !== null", timeout=5_000)
    filled = page.evaluate(
        "(() => { const r = document.querySelector('#player video').getBoundingClientRect();"
        " return r.height / window.innerHeight; })()"
    )
    assert filled > 0.95, f"the video fills {filled:.0%} of the screen's height"
    page.evaluate("document.exitFullscreen()")

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


@pytest.mark.parametrize("preset", ["bw", "warm", "cool", "vintage", "vivid"])
def test_a_person_can_preview_process_and_play_a_filter_preset(page, preset):
    """The chosen look crosses the real API, queue and FFmpeg, then plays."""
    sign_up(page)

    page.set_input_files("#file-input", str(VIDEO))
    page.wait_for_function(
        "document.getElementById('status').textContent === 'Processing complete.'",
        timeout=PROCESSING_TIMEOUT_MS,
    )
    # Let the original's HLS ladder land too. A same-origin MediaSource is
    # readable by WebGL, unlike an MP4 fetched without storage CORS.
    page.wait_for_function(
        "document.querySelector('#player video')?.currentSrc.startsWith('blob:')",
        timeout=PROCESSING_TIMEOUT_MS,
    )

    page.click("#nav-library")
    page.wait_for_selector("#library-grid .card-edit")
    page.click("#library-grid .card-edit")
    page.wait_for_selector("#view-edit:not([hidden])")

    page.click('details[data-operation="preset"] summary')
    page.check("#edit-preset")
    page.wait_for_selector(".preset-panel")
    assert page.locator("#edit-process").is_disabled()

    page.click(f'.preset-tile[data-preset="{preset}"]')
    assert (
        page.locator(f'.preset-tile[data-preset="{preset}"]').get_attribute("aria-checked")
        == "true"
    )
    page.wait_for_function(
        """() => {
            const canvas = document.querySelector('.preset-preview');
            const video = document.querySelector('#edit-player video');
            return (canvas && !canvas.hidden) || (video && video.style.filter !== '');
        }""",
        timeout=30_000,
    )
    assert page.locator("#edit-process").is_enabled()

    with page.expect_response(
        lambda response: response.request.method == "POST" and response.url.endswith("/edit")
    ) as sent:
        page.click("#edit-process")
    response = sent.value
    assert response.status == 202, response.text()
    assert response.request.post_data_json == {
        "operations": [{"operation": "preset", "params": {"name": preset}}]
    }
    result_id = response.json()["job_id"]
    assert result_id not in response.url
    page.wait_for_function(
        "document.querySelector('#edit-status').textContent.startsWith('Done')",
        timeout=PROCESSING_TIMEOUT_MS,
    )
    page.wait_for_function(
        "document.querySelector('#edit-result-player video')?.readyState >= 2",
        timeout=30_000,
    )
    page.locator("#edit-result-player video").evaluate("video => video.play()")
    page.wait_for_function(
        "document.querySelector('#edit-result-player video').currentTime > 0.1",
        timeout=15_000,
    )
    assert page.locator("#edit-result-player video").evaluate("video => video.error") is None
    result = page.request.get(f"{BASE_URL}/api/jobs/{result_id}").json()
    assert result["status"] == "done"
    assert result["filename"].endswith("-edited.mp4")
    assert result["output_url"]
    downloaded = page.request.get(result["output_url"])
    assert downloaded.ok
    probed = subprocess.run(
        [
            *_media_command("ffprobe"),
            "-v",
            "error",
            "-i",
            "pipe:0",
            "-show_streams",
            "-show_format",
            "-of",
            "json",
        ],
        input=downloaded.body(),
        capture_output=True,
        check=True,
        timeout=30,
    )
    media = json.loads(probed.stdout)
    video = next(stream for stream in media["streams"] if stream["codec_type"] == "video")
    assert (video["width"], video["height"]) == (640, 360)
    assert video["pix_fmt"] == "yuv420p"
    assert video["color_space"] == "bt709"
    assert any(stream["codec_type"] == "audio" for stream in media["streams"])
    assert float(media["format"]["duration"]) == pytest.approx(3, abs=0.15)

    unexpected = [
        text
        for text in page.console_errors
        if not any(e in text for e in EXPECTED_CONSOLE + EXPECTED_PRESET_CONSOLE)
    ]
    assert unexpected == []
