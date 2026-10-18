# Track C — the clip scrubber (sprint 5)

The editor has had a Clip section since sprint 3, disabled with "Coming in the
next update". This is the component that fills it, built to the contract in
[`frontend/EDITOR-COMPONENTS.md`](../frontend/EDITOR-COMPONENTS.md) and
[`sprint5-plan.md`](sprint5-plan.md) §2.2.

| File | What |
|---|---|
| `frontend/clip-scrubber.js` | `window.mountClipScrubber(video, onChange)` — a plain script, like `app.js` |
| `frontend/clip-scrubber.css` | Its styles; only the colour tokens come from `style.css` |
| `frontend/clip-scrubber.test.js` | 35 tests, jsdom |

`index.html` loads both files before `app.js`. Nothing in `app.js` changed: the
panel already looked the global up and already sent `{ start, end }` as the
clip operation's params.

## What it promises

- **`onChange({ start, end })` in seconds on the playing video's timeline**,
  with `0 <= start < end <= duration` — the three things the worker's
  `validate_clip` refuses otherwise. No arrangement of drags or keys breaks it.
- **Nothing is reported until the reader moves a handle.** A ticked Clip with
  no range keeps Process disabled, which is the panel's existing rule.
- **The end never passes the length, rounded down to a tenth.** The browser's
  idea of the length and the worker's ffprobe can differ by a few
  milliseconds; rounding down keeps "clip to the end" inside the worker's.

## How it behaves

- Two handles over the timeline, the range between them highlighted, the
  player's position drawn as a thin line, and a readout under it:
  `0:03.0 – 0:08.5   5.5 s`.
- **Proportional on any length.** Up to a minute it works in tenths of a second
  and the readout shows them; longer than that a pixel of track is already more
  than a second, so it works in whole seconds and shows plain `m:ss`. The
  readout is exactly what is sent.
- **The player seeks as a handle moves**, and a playing video pauses when a
  drag starts, so the frame on screen is the frame at the handle.
- **A press on the bare track** moves the nearer handle there. A handle is
  dragged by where it was held, not by its centre.
- **Two handles on top of each other** (a one-second clip of a ten-minute
  video is under a pixel) part in the direction of the first move: left takes
  the start, right the end. Otherwise one could never be got out from under
  the other.
- **Keyboard**, on a focused handle (`role="slider"`, with `aria-valuetext` as
  `m:ss`): arrows step by the grid, Shift by ten steps, Page Up/Down by a tenth
  of the video, Home/End as far as that handle can go.
- **Before the length is known** (`preload=metadata` still loading, or hls.js
  still parsing) the handles are disabled and the readout says so. A video
  shorter than one step says it is too short to clip.

## Verified

- The 35 tests, and the same file run in Chromium against the real component.
- End to end on a local stack: a 12-second upload, Clip opened in the editor,
  handles dragged to 3.0 s and 8.5 s, one key press each way on the end handle.
  The panel sent `{"operation":"clip","params":{"start":3,"end":8.5}}`, the
  worker finished it, and the result is 5.500 s by ffprobe — the 5.5 s the
  readout showed. A frame 0.1 s into the result shows the source's clock at
  00:00:03.100, and one 0.2 s before its end shows 00:00:08.300.

## Not in this

**Editing from the original** (§2.2's root-job chain and `edit_source`) is
paired with the crop box by §3 — both ship or neither does. The scrubber is
not bound by that rule: it measures whatever video the editor plays, and the
edit runs on that same video either way.
