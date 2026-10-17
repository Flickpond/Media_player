// @vitest-environment jsdom
//
// Tests for clip-scrubber.js, the component the editor mounts through
// `window.mountClipScrubber(video, onChange)` (EDITOR-COMPONENTS.md). The
// panel side of that seam is covered in app.test.js with a stand-in; these
// are about the component's own promise: what it reports is the range the
// reader sees, in the video's seconds, with 0 <= start < end <= duration.
//
// jsdom has no layout and no media pipeline, so the track's box and the
// video's duration and clock are given here; everything else is the real
// component driven through the events a browser would send.

import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import "./clip-scrubber.js";

const TRACK_LEFT = 100;
const TRACK_WIDTH = 400;

let host;
let video;
let onChange;
let cleanup;

/** A <video> whose length and clock the test controls. */
function fakeVideo(initialDuration) {
  const element = document.createElement("video");
  let duration = initialDuration;
  let current = 0;
  let paused = true;
  Object.defineProperty(element, "duration", { configurable: true, get: () => duration });
  Object.defineProperty(element, "currentTime", {
    configurable: true,
    get: () => current,
    set: (value) => {
      current = value;
    },
  });
  Object.defineProperty(element, "paused", { configurable: true, get: () => paused });
  element.pause = vi.fn(() => {
    paused = true;
  });
  element.play = () => {
    paused = false;
  };
  element.setDuration = (value) => {
    duration = value;
    element.dispatchEvent(new Event("durationchange"));
  };
  element.setClock = (value) => {
    current = value;
    element.dispatchEvent(new Event("timeupdate"));
  };
  return element;
}

function mount(duration) {
  video = fakeVideo(duration);
  document.body.append(video);
  cleanup = window.mountClipScrubber(video, onChange, host);
  track().getBoundingClientRect = () => ({
    left: TRACK_LEFT,
    right: TRACK_LEFT + TRACK_WIDTH,
    width: TRACK_WIDTH,
    top: 0,
    bottom: 34,
    height: 34,
    x: TRACK_LEFT,
    y: 0,
  });
  return video;
}

const track = () => host.querySelector(".clip-track");
const startHandle = () => host.querySelector(".clip-handle-start");
const endHandle = () => host.querySelector(".clip-handle-end");
const readout = () => host.querySelector(".clip-readout").textContent;
const lastReport = () => onChange.mock.calls.at(-1)[0];
/** The x on the track for a fraction of its width. */
const at = (fraction) => TRACK_LEFT + TRACK_WIDTH * fraction;

function pointer(target, type, clientX) {
  const event = new MouseEvent(type, { bubbles: true, cancelable: true, clientX, button: 0 });
  target.dispatchEvent(event);
  return event;
}

/** Press on `target`, move through `xs`, let go. */
function drag(target, fromX, ...xs) {
  pointer(target, "pointerdown", fromX);
  for (const x of xs) pointer(document, "pointermove", x);
  pointer(document, "pointerup", xs.at(-1) ?? fromX);
}

function key(target, name, options = {}) {
  const event = new KeyboardEvent("keydown", { key: name, bubbles: true, cancelable: true, ...options });
  target.dispatchEvent(event);
  return event;
}

beforeEach(() => {
  document.body.innerHTML = "";
  host = document.createElement("div");
  document.body.append(host);
  onChange = vi.fn();
  cleanup = null;
});

afterEach(() => {
  if (cleanup) cleanup();
  document.body.innerHTML = "";
});

describe("clip scrubber", () => {
  it("is the global the editor looks up", () => {
    expect(typeof window.mountClipScrubber).toBe("function");
  });

  it("draws into the editor's clip section when no host is given", () => {
    const section = document.createElement("div");
    section.id = "edit-clip-mount";
    document.body.append(section);
    const element = fakeVideo(10);
    document.body.append(element);

    cleanup = window.mountClipScrubber(element, onChange);

    expect(section.querySelector(".clip-scrubber")).not.toBeNull();
  });

  it("draws right after the player when the page has no clip section", () => {
    const player = document.createElement("div");
    player.className = "player-host";
    const element = fakeVideo(10);
    player.append(element);
    document.body.append(player);

    cleanup = window.mountClipScrubber(element, onChange);

    expect(player.nextElementSibling.querySelector(".clip-scrubber")).not.toBeNull();
  });

  it("draws right after a bare video element as a last resort", () => {
    const element = fakeVideo(10);
    document.body.append(element);

    cleanup = window.mountClipScrubber(element, onChange);

    expect(element.nextElementSibling.querySelector(".clip-scrubber")).not.toBeNull();
  });

  it("offers the whole video and reports nothing until the reader moves a handle", () => {
    mount(10);

    expect(readout()).toContain("0:00.0 – 0:10.0");
    expect(readout()).toContain("10.0 s");
    expect(startHandle().getAttribute("aria-valuenow")).toBe("0");
    expect(endHandle().getAttribute("aria-valuenow")).toBe("10");
    // A ticked Clip with no range keeps Process disabled; reporting the
    // whole video here would send a clip that cuts nothing.
    expect(onChange).not.toHaveBeenCalled();
  });

  it("waits for the video's length before it can be used", () => {
    mount(Number.NaN);

    expect(readout()).toContain("Loading");
    expect(startHandle().getAttribute("aria-disabled")).toBe("true");
    expect(startHandle().tabIndex).toBe(-1);
    drag(track(), at(0.5), at(0.6));
    key(startHandle(), "ArrowRight");
    expect(onChange).not.toHaveBeenCalled();

    video.setDuration(8);

    expect(readout()).toContain("0:00.0 – 0:08.0");
    expect(startHandle().getAttribute("aria-disabled")).toBe("false");
    expect(startHandle().tabIndex).toBe(0);
  });

  it("says when a video is too short to clip", () => {
    mount(0.05);

    expect(readout()).toContain("too short");
    expect(host.querySelector(".clip-scrubber").classList.contains("is-disabled")).toBe(true);
  });

  it("reports a dragged start in the video's seconds and seeks the player to it", () => {
    mount(10);
    video.play();

    drag(startHandle(), at(0), at(0.25));

    expect(lastReport()).toEqual({ start: 2.5, end: 10 });
    expect(video.currentTime).toBe(2.5);
    expect(video.pause).toHaveBeenCalled();
    expect(readout()).toContain("0:02.5 – 0:10.0");
    expect(readout()).toContain("7.5 s");
    expect(parseFloat(host.querySelector(".clip-range").style.left)).toBeCloseTo(25);
    expect(parseFloat(host.querySelector(".clip-range").style.width)).toBeCloseTo(75);
  });

  it("drags a handle by where it was held, not by its centre", () => {
    mount(10);

    // Held 4 px right of its centre (0.1 s on this track): moving 40 px
    // moves it 1 s, not 1.1 s.
    drag(endHandle(), at(1) + 4, at(1) - 36);

    expect(lastReport()).toEqual({ start: 0, end: 9 });
  });

  it("measures a ten-minute video in whole seconds on the same track", () => {
    mount(600);

    drag(endHandle(), at(1), at(0.5));
    expect(lastReport()).toEqual({ start: 0, end: 300 });

    drag(startHandle(), at(0), at(0.1));
    expect(lastReport()).toEqual({ start: 60, end: 300 });
    expect(readout()).toContain("1:00 – 5:00");
    expect(readout()).toContain("4:00");
    expect(startHandle().getAttribute("aria-valuetext")).toBe("1:00");
  });

  it("is proportional on a ten-second video", () => {
    mount(10);

    drag(endHandle(), at(1), at(0.5));

    expect(lastReport()).toEqual({ start: 0, end: 5 });
    expect(parseFloat(endHandle().style.left)).toBeCloseTo(50);
  });

  it("cannot be dragged into start >= end", () => {
    mount(10);
    drag(endHandle(), at(1), at(0.5));
    drag(startHandle(), at(0), at(0.9));
    expect(lastReport()).toEqual({ start: 4.9, end: 5 });

    cleanup();
    mount(10);
    drag(startHandle(), at(0), at(0.5));
    drag(endHandle(), at(1), at(0));
    expect(lastReport()).toEqual({ start: 5, end: 5.1 });

    for (const [args] of onChange.mock.calls) expect(args.start < args.end).toBe(true);
  });

  it("keeps a long video's shortest clip to one whole second", () => {
    mount(600);
    drag(endHandle(), at(1), at(0.5));

    drag(startHandle(), at(0), at(0.9));

    expect(lastReport()).toEqual({ start: 299, end: 300 });
  });

  it("keeps the range inside the video however far the pointer goes", () => {
    mount(10);

    drag(startHandle(), at(0), at(0.3), at(-0.5));
    expect(lastReport().start).toBe(0);

    drag(endHandle(), at(1), at(0.7), at(1.8));
    expect(lastReport().end).toBe(10);
  });

  it("never offers an end past the length the video reports", () => {
    // The worker refuses end > its own probe of the file, so the last stop
    // is the length rounded down to a tenth, never up.
    mount(10.06);

    expect(endHandle().getAttribute("aria-valuenow")).toBe("10");
    key(endHandle(), "ArrowLeft");
    key(endHandle(), "End");
    expect(lastReport()).toEqual({ start: 0, end: 10 });
  });

  it("lets the end reach the very end of a long video between whole seconds", () => {
    mount(600.4);

    drag(endHandle(), at(1), at(0.5), at(1));

    expect(lastReport()).toEqual({ start: 0, end: 600.4 });
    expect(readout()).toContain("10:00");
  });

  it("moves the nearer handle to a press on the bare track", () => {
    mount(10);

    drag(track(), at(0.2));
    expect(lastReport()).toEqual({ start: 2, end: 10 });

    drag(track(), at(0.8));
    expect(lastReport()).toEqual({ start: 2, end: 8 });
    expect(video.currentTime).toBe(8);
  });

  it("parts two handles on top of each other in the direction of the drag", () => {
    mount(600);
    drag(endHandle(), at(1), at(0.5));
    drag(startHandle(), at(0), at(0.9));
    expect(lastReport()).toEqual({ start: 299, end: 300 });

    // The two now overlap: a press there takes neither until it moves, and
    // a move to the left takes the start -- by where it was held, a second
    // right of the start's own time.
    drag(endHandle(), at(0.5), at(0.5), at(0.25));
    expect(lastReport()).toEqual({ start: 149, end: 300 });

    cleanup();
    mount(600);
    drag(endHandle(), at(1), at(0.5));
    drag(startHandle(), at(0), at(0.9));

    // ...and a move to the right takes the end.
    drag(track(), at(0.5), at(0.75));
    expect(lastReport()).toEqual({ start: 299, end: 450 });
  });

  it("ignores buttons other than the main one", () => {
    mount(10);

    track().dispatchEvent(
      new MouseEvent("pointerdown", { bubbles: true, clientX: at(0.5), button: 2 }),
    );
    pointer(document, "pointermove", at(0.6));

    expect(onChange).not.toHaveBeenCalled();
  });

  it("stops following the pointer once it is let go", () => {
    mount(10);
    drag(startHandle(), at(0), at(0.1));
    const reports = onChange.mock.calls.length;

    pointer(document, "pointermove", at(0.4));

    expect(onChange).toHaveBeenCalledTimes(reports);
  });

  it("steps a focused handle with the arrow keys", () => {
    mount(10);

    const event = key(startHandle(), "ArrowRight");
    expect(event.defaultPrevented).toBe(true);
    expect(lastReport()).toEqual({ start: 0.1, end: 10 });

    key(startHandle(), "ArrowUp", { shiftKey: true });
    expect(lastReport()).toEqual({ start: 1.1, end: 10 });

    key(startHandle(), "ArrowDown");
    expect(lastReport()).toEqual({ start: 1, end: 10 });

    key(endHandle(), "ArrowLeft", { shiftKey: true });
    expect(lastReport()).toEqual({ start: 1, end: 9 });

    key(endHandle(), "PageDown");
    expect(lastReport()).toEqual({ start: 1, end: 8 });

    key(startHandle(), "PageUp");
    expect(lastReport()).toEqual({ start: 2, end: 8 });
    expect(video.currentTime).toBe(2);
  });

  it("sends Home and End as far as that handle can go", () => {
    mount(10);
    key(startHandle(), "PageUp");
    key(endHandle(), "PageDown");

    key(startHandle(), "End");
    expect(lastReport()).toEqual({ start: 8.9, end: 9 });

    key(endHandle(), "Home");
    expect(lastReport()).toEqual({ start: 8.9, end: 9 });

    key(startHandle(), "Home");
    key(endHandle(), "End");
    expect(lastReport()).toEqual({ start: 0, end: 10 });
  });

  it("steps a long video by whole seconds", () => {
    mount(600);

    key(startHandle(), "ArrowRight");
    expect(lastReport()).toEqual({ start: 1, end: 600 });

    key(startHandle(), "ArrowRight", { shiftKey: true });
    expect(lastReport()).toEqual({ start: 11, end: 600 });
  });

  it("leaves keys it does not use to the page", () => {
    mount(10);

    const event = key(startHandle(), "Enter");

    expect(event.defaultPrevented).toBe(false);
    expect(onChange).not.toHaveBeenCalled();
  });

  it("does not report a key that cannot move the handle any further", () => {
    mount(10);
    key(startHandle(), "ArrowRight");
    key(startHandle(), "ArrowLeft");
    const reports = onChange.mock.calls.length;

    key(startHandle(), "ArrowLeft");

    expect(onChange).toHaveBeenCalledTimes(reports);
  });

  it("holds on to the pointer when the browser can capture it", () => {
    mount(10);
    const captured = [];
    track().setPointerCapture = (id) => captured.push(id);
    const press = new MouseEvent("pointerdown", { bubbles: true, cancelable: true, clientX: at(0) });
    Object.defineProperty(press, "pointerId", { value: 7 });

    startHandle().dispatchEvent(press);
    pointer(document, "pointermove", at(0.3));
    pointer(document, "pointerup", at(0.3));

    expect(captured).toEqual([7]);
    expect(lastReport()).toEqual({ start: 3, end: 10 });
  });

  it("still drags when capturing the pointer fails", () => {
    mount(10);
    track().setPointerCapture = () => {
      throw new Error("InvalidPointerId");
    };
    const press = new MouseEvent("pointerdown", { bubbles: true, cancelable: true, clientX: at(0) });
    Object.defineProperty(press, "pointerId", { value: 9 });

    startHandle().dispatchEvent(press);
    pointer(document, "pointermove", at(0.4));
    pointer(document, "pointerup", at(0.4));

    expect(lastReport()).toEqual({ start: 4, end: 10 });
  });

  it("ignores a press on a track that has no width yet", () => {
    mount(10);
    track().getBoundingClientRect = () => ({ left: 0, width: 0, right: 0, top: 0, bottom: 0, height: 0 });

    drag(track(), 50, 80);

    expect(onChange).not.toHaveBeenCalled();
  });

  it("hides the playhead while the player has no position", () => {
    mount(10);

    video.setClock(Number.NaN);

    expect(host.querySelector(".clip-playhead").hidden).toBe(true);
  });

  it("follows the playhead along the track", () => {
    mount(10);
    const playhead = host.querySelector(".clip-playhead");

    video.setClock(4);

    expect(playhead.hidden).toBe(false);
    expect(parseFloat(playhead.style.left)).toBeCloseTo(40);
  });

  it("keeps a selection inside a video whose length changes under it", () => {
    mount(10);
    key(endHandle(), "End");
    for (let i = 0; i < 9; i += 1) key(startHandle(), "PageUp");
    expect(lastReport()).toEqual({ start: 9, end: 10 });

    video.setDuration(6);

    expect(lastReport()).toEqual({ start: 5.9, end: 6 });
  });

  it("does not redraw or report when the player repeats the same length", () => {
    mount(10);
    key(startHandle(), "PageUp");
    const reports = onChange.mock.calls.length;

    video.dispatchEvent(new Event("loadedmetadata"));

    expect(onChange).toHaveBeenCalledTimes(reports);
    expect(lastReport()).toEqual({ start: 1, end: 10 });
  });

  it("keeps offering the whole video while nothing is selected and the length changes", () => {
    mount(10);

    video.setDuration(12);

    expect(readout()).toContain("0:00.0 – 0:12.0");
    expect(onChange).not.toHaveBeenCalled();
  });

  it("keeps seeking a player that throws while it is not ready", () => {
    mount(10);
    Object.defineProperty(video, "currentTime", {
      configurable: true,
      get: () => 0,
      set: () => {
        throw new Error("not seekable yet");
      },
    });

    key(startHandle(), "ArrowRight");

    expect(lastReport()).toEqual({ start: 0.1, end: 10 });
  });

  it("removes itself and stops listening when the editor closes", () => {
    mount(10);
    pointer(startHandle(), "pointerdown", at(0));

    cleanup();
    cleanup = null;

    expect(host.querySelector(".clip-scrubber")).toBeNull();
    pointer(document, "pointermove", at(0.5));
    video.setDuration(20);
    expect(onChange).not.toHaveBeenCalled();
  });
});
