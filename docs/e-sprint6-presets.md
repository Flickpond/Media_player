# Sprint 6 — Track E: filter presets

Verified locally on 21 October 2026. The five looks (`bw`, `warm`, `cool`,
`vintage`, `vivid`) are implemented from schema and validation through FFmpeg,
the editor panel, preview, and a playable new library entry.

## Preview and result

The first preview used approximate RGB formulas. It now uses a 32-point RGB
cube for each look, generated with the worker's actual filter chain. The five
cubes occupy one 150,351-byte PNG. WebGL interpolates the tables while the
video plays; a look never sends a processing request until **Process**.

An additional real BT.709 YUV check caught a colour-matrix problem: a black
and white preview differed from FFmpeg by 10.06/255 on average. The worker
now converts to BT.709 before the colour filters, rather than just changing
the tag. The final conversion explicitly produces limited-range BT.709
`yuv420p`, including after RGB filters. This keeps HD, tone-mapped HDR and
ordinary SDR on the same colour transform and keeps the result playable.

The following comparisons use the actual browser GPU output against actual
FFmpeg output, not a JavaScript reimplementation of the shader. The fixtures
are a smooth RGB gradient, a BT.709 YUV version of it, and a decoded frame of
`tests/browser/smoke.mp4`. The latter is a generated 3-second test video,
not camera footage. Numbers are PSNR in dB; higher is closer.

| Look | RGB gradient | BT.709 YUV gradient | Decoded video frame |
|---|---:|---:|---:|
| B&W | 44.40 | 45.09 | 49.37 |
| Warm | 43.23 | 44.50 | 39.22 |
| Cool | 43.23 | 44.61 | 39.03 |
| Vintage | 44.16 | 44.62 | 40.51 |
| Vivid | 42.78 | 44.16 | 38.71 |

Mean absolute channel errors are 0.50–1.39 out of 255. The regression gate is
PSNR above 30 dB **and** mean error below 4/255 for every case. Tables are an
interpolation approximation; compression, chroma subsampling and the original
upload's tone map can introduce additional differences in a processed video.

If WebGL, the colour-table download, or readable storage pixels are unavailable,
the panel retains a CSS preview and explicitly labels it **Approximate preview**.
GPU context loss follows the same path. The chosen operation remains usable.

## Browser acceptance

The local Compose stack was running its real API, PostgreSQL, Redis, MinIO
and two workers; workers were rebuilt after the colour-space fix. For each
of the five presets, Chromium:

1. Creates an account, uploads the fixture and waits for its HLS ladder.
2. Opens the editor, enables Presets and chooses the look.
3. Clicks **Process** and checks the actual POST body and HTTP 202 response.
4. Waits for the worker result and plays it with an advancing timeline.
5. Downloads the MP4 and probes it: 640×360, `yuv420p`, BT.709, an audio
   stream and approximately 3 seconds, with a new job ID and library filename.

The 21 browser cases passed: 15 colour comparisons, five complete preset
jobs, and the existing signup/upload/play/crop smoke test. These ran as the
colour-comparison group and the stack-dependent group separately.

Component regression tests also cover keyboard selection, reset, unmount,
late colour-table responses, download failure, an unreadable video texture,
GPU loss, and unticking/re-ticking. A selection changed while unticked must
be the selection shown when enabled again; playback/seek cannot resurrect an
unticked preview.

Desktop visual check: [editor with Vintage selected](assets/e-sprint6-presets-desktop.png).
At a 390-pixel CSS viewport the preset tiles and Process button also fit
within the viewport; the preview canvas matches the video's bounds.

## Repeat the checks

From the repository root, with development dependencies installed:

```sh
python -m ruff check .
python -m pytest -q --cov=app
cd frontend
npm run test:coverage
```

Final local results: **811 Python passed, 99 skipped**, application coverage
**92.40%**; **283 frontend passed, 3 live tests skipped**. Frontend branch
coverage is **94.12%** overall and **90.67%** for `preset.js`.

One loaded full-suite run timed out in the existing worker-interruption
process-stop timing test. That test passed in isolation, and the subsequent
full-suite run passed. No interruption code or timeout was changed.

With the local stack up, Chromium installed and Playwright available:

```sh
RUN_BROWSER_TESTS=1 python -m pytest -q -s --noconftest tests/browser
```

On Windows, set `RUN_BROWSER_TESTS` in PowerShell and set
`PLAYWRIGHT_BROWSERS_PATH` if Chromium is in a project-local directory. The
colour checks and probes use host FFmpeg/ffprobe when available, otherwise
`docker compose exec -T worker`. The browser CI job already starts that stack.

## Regenerate the tables

```sh
python frontend/scripts/generate-preset-luts.py
# Or use a running local worker's FFmpeg:
python frontend/scripts/generate-preset-luts.py --container flickpond-worker-1
```

The generator imports `build()` from the worker's preset module and samples
solid colour tiles, keeping chroma subsampling from mixing adjacent lookup
points. Commit the PNG and JSON manifest together. A unit test rejects a
manifest with stale worker filters or a PNG with a different hash.

## Review and release

The shared changes are the agreed schema/rule/filter registrations, script
and stylesheet loading, frontend coverage/Sonar entries, and the editor
browser test. The rest is in E's own preset files and this verification note.

Both the core/frontend **and** the worker image need deployment; deploying
only the page leaves an API/worker version that may reject the operation.
To hide the feature before freeze, add `hidden` to the
`details[data-operation="preset"]` section in `frontend/index.html` and deploy
the core. Existing preset jobs remain valid.

These results are local acceptance. No production or staging deployment was
performed in this work, and no production acceptance is claimed. After review
and deployment, repeat an actual upload, preset, Process and playback on
flickpond.com; include camera footage and a real HDR display in that check.
