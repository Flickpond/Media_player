// @vitest-environment jsdom
//
// Tests for crop-box.js, the component the editor mounts through
// `window.mountCropBox(video, onChange, options)` (EDITOR-COMPONENTS.md). The
// panel side of that seam is covered in app.test.js with a stand-in; these
// are about the component's own promise: what it reports is the box the
// reader drew, in the original's pixels, whole, even, and inside the frame.
//
// jsdom has no layout and no media pipeline, so the stage's box and the
// video's size are given here; everything else is the real component driven
// through the events a browser would send.

import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import "./crop-box.js";

// A 400 x 225 stage over a 3840 x 2160 frame: one stage pixel is 9.6 source
// pixels, so every expected number below is a stage position times 9.6.
const STAGE = { left: 100, top: 50, width: 400, height: 225 };
const SOURCE = { width: 3840, height: 2160 };

let host;
let video;
let onChange;
let cleanup;
let drawImage;

/** A <video> whose rendition size the test controls. */
function fakeVideo({ videoWidth = 640, videoHeight = 360 } = {}) {
  const element = document.createElement("video");
  let paused = false;
  let size = { videoWidth, videoHeight };
  Object.defineProperty(element, "videoWidth", { configurable: true, get: () => size.videoWidth });
  Object.defineProperty(element, "videoHeight", {
    configurable: true,
    get: () => size.videoHeight,
  });
  Object.defineProperty(element, "readyState", { configurable: true, get: () => 4 });
  Object.defineProperty(element, "paused", { configurable: true, get: () => paused });
  element.pause = vi.fn(() => {
    paused = true;
  });
  element.setSize = (width, height) => {
    size = { videoWidth: width, videoHeight: height };
    element.dispatchEvent(new Event("resize"));
  };
  return element;
}

function mount(options = { ...SOURCE, host: undefined }, videoOptions) {
  video = fakeVideo(videoOptions);
  document.body.append(video);
  cleanup = window.mountCropBox(video, onChange, { ...options, host: options.host ?? host });
  stage().getBoundingClientRect = () => ({
    ...STAGE,
    right: STAGE.left + STAGE.width,
    bottom: STAGE.top + STAGE.height,
    x: STAGE.left,
    y: STAGE.top,
  });
  return video;
}

const stage = () => host.querySelector(".crop-stage");
const selection = () => host.querySelector(".crop-selection");
const handle = (corner) => host.querySelector(`.crop-handle-${corner}`);
const readout = () => host.querySelector(".crop-readout").textContent;
const lastReport = () => onChange.mock.calls.at(-1)[0];
/** Client coordinates for a fraction across and down the stage. */
const at = (fx, fy) => [STAGE.left + STAGE.width * fx, STAGE.top + STAGE.height * fy];

function pointer(target, type, [clientX, clientY]) {
  const event = new MouseEvent(type, {
    bubbles: true,
    cancelable: true,
    clientX,
    clientY,
    button: 0,
  });
  target.dispatchEvent(event);
  return event;
}

function drag(target, from, ...points) {
  pointer(target, "pointerdown", from);
  for (const point of points) pointer(document, "pointermove", point);
  pointer(document, "pointerup", points.at(-1) ?? from);
}

function key(name, options = {}) {
  const event = new KeyboardEvent("keydown", {
    key: name,
    bubbles: true,
    cancelable: true,
    ...options,
  });
  selection().dispatchEvent(event);
  return event;
}

function isLegal({ x, y, w, h }) {
  return (
    [x, y, w, h].every(Number.isInteger) &&
    w % 2 === 0 &&
    h % 2 === 0 &&
    x >= 0 &&
    y >= 0 &&
    w > 0 &&
    h > 0 &&
    x + w <= SOURCE.width &&
    y + h <= SOURCE.height
  );
}

beforeEach(() => {
  document.body.innerHTML = "";
  host = document.createElement("div");
  document.body.append(host);
  onChange = vi.fn();
  cleanup = null;
  // jsdom has no 2D canvas; the still is the only thing that needs one.
  drawImage = vi.fn();
  vi.spyOn(HTMLCanvasElement.prototype, "getContext").mockImplementation(() => ({ drawImage }));
});

afterEach(() => {
  if (cleanup) cleanup();
  document.body.innerHTML = "";
  vi.restoreAllMocks();
});

describe("crop box", () => {
  it("is the global the editor looks up", () => {
    expect(typeof window.mountCropBox).toBe("function");
  });

  it("draws into the editor's crop section when no host is given", () => {
    const section = document.createElement("div");
    section.id = "edit-crop-mount";
    document.body.append(section);
    const element = fakeVideo();
    document.body.append(element);

    cleanup = window.mountCropBox(element, onChange);

    expect(section.querySelector(".crop-box")).not.toBeNull();
  });

  it("draws right after the player when the page has no crop section", () => {
    const player = document.createElement("div");
    player.className = "player-host";
    const element = fakeVideo();
    player.append(element);
    document.body.append(player);

    cleanup = window.mountCropBox(element, onChange);

    expect(player.nextElementSibling.querySelector(".crop-box")).not.toBeNull();
  });

  it("reports nothing until the reader draws, so a ticked Crop stays unset", () => {
    mount();

    expect(onChange).not.toHaveBeenCalled();
    expect(readout()).toContain("3840 × 2160");
  });

  it("takes the original's size over the smaller rendition that is playing", () => {
    mount(SOURCE, { videoWidth: 640, videoHeight: 360 });

    drag(stage(), at(0.25, 0.25), at(0.75, 0.75));

    expect(lastReport()).toEqual({ x: 960, y: 540, w: 1920, h: 1080 });
  });

  it("falls back to the element's own size when the original's is not known", () => {
    mount({}, { videoWidth: 1280, videoHeight: 720 });

    drag(stage(), at(0, 0), at(0.5, 0.5));

    expect(lastReport()).toEqual({ x: 0, y: 0, w: 640, h: 360 });
  });

  it("waits for the video's size before it can be used", () => {
    mount({}, { videoWidth: 0, videoHeight: 0 });

    expect(host.querySelector(".crop-box").classList.contains("is-disabled")).toBe(true);
    expect(readout()).toContain("Loading");
    drag(stage(), at(0, 0), at(0.5, 0.5));
    expect(onChange).not.toHaveBeenCalled();

    video.setSize(1920, 1080);
    expect(host.querySelector(".crop-box").classList.contains("is-disabled")).toBe(false);
  });

  it("draws a new box on a press inside a box that still fills the frame", () => {
    // In a browser the whole-frame box covers the stage, so the first press
    // always lands on it. Treating that as a move meant nothing could be drawn.
    mount();

    drag(selection(), at(0.25, 0.25), at(0.75, 0.75));

    expect(lastReport()).toEqual({ x: 960, y: 540, w: 1920, h: 1080 });
  });

  it("draws a box dragged up and to the left as well as down and to the right", () => {
    mount();

    drag(stage(), at(0.75, 0.75), at(0.25, 0.25));

    expect(lastReport()).toEqual({ x: 960, y: 540, w: 1920, h: 1080 });
  });

  it("keeps every box whole, even and inside the frame, however it is dragged", () => {
    mount();

    // Off the stage in every direction, at awkward fractions.
    drag(stage(), at(0.333, 0.271), at(1.4, 1.3));
    drag(stage(), at(0.611, 0.517), at(-0.2, -0.4));
    drag(selection(), at(0.3, 0.3), at(0.9, 0.95), at(2, 2));

    for (const [report] of onChange.mock.calls) expect(isLegal(report), JSON.stringify(report)).toBe(true);
  });

  it("ignores a click with no drag rather than shrinking the box to nothing", () => {
    mount();

    drag(stage(), at(0.5, 0.5), at(0.501, 0.501));

    expect(onChange).not.toHaveBeenCalled();
  });

  it("never makes a box smaller than the minimum", () => {
    mount();

    drag(stage(), at(0.5, 0.5), at(0.505, 0.51));

    expect(lastReport().w).toBeGreaterThanOrEqual(32);
    expect(lastReport().h).toBeGreaterThanOrEqual(32);
  });

  it("moves the whole box when it is dragged from inside, stopping at the edge", () => {
    mount();
    drag(stage(), at(0.25, 0.25), at(0.5, 0.5));
    onChange.mockClear();

    drag(selection(), at(0.3, 0.3), at(0.4, 0.3));
    expect(lastReport()).toEqual({ x: 1344, y: 540, w: 960, h: 540 });

    drag(selection(), at(0.5, 0.5), at(3, 3));
    expect(lastReport()).toEqual({ x: 2880, y: 1620, w: 960, h: 540 });
  });

  it("resizes from a corner and keeps the opposite corner where it was", () => {
    mount();
    drag(stage(), at(0.25, 0.25), at(0.75, 0.75));

    drag(handle("nw"), at(0.25, 0.25), at(0.5, 0.5));
    expect(lastReport()).toEqual({ x: 1920, y: 1080, w: 960, h: 540 });

    drag(handle("se"), at(0.75, 0.75), at(1, 1));
    expect(lastReport()).toEqual({ x: 1920, y: 1080, w: 1920, h: 1080 });

    drag(handle("ne"), at(1, 0.5), at(0.75, 0.25));
    expect(lastReport()).toEqual({ x: 1920, y: 540, w: 960, h: 1620 });

    drag(handle("sw"), at(0.5, 1), at(0, 0.75));
    expect(lastReport()).toEqual({ x: 0, y: 540, w: 2880, h: 1080 });
  });

  it("holds the chosen shape, centred on the box it had", () => {
    mount();
    drag(stage(), at(0, 0), at(1, 1));

    const aspect = host.querySelector(".crop-aspect");
    aspect.value = "1:1";
    aspect.dispatchEvent(new Event("change"));

    expect(lastReport()).toEqual({ x: 840, y: 0, w: 2160, h: 2160 });

    drag(stage(), at(0.1, 0.1), at(0.9, 0.4));
    const { w, h } = lastReport();
    expect(w).toBe(h);

    aspect.value = "9:16";
    aspect.dispatchEvent(new Event("change"));
    expect(lastReport().w / lastReport().h).toBeCloseTo(9 / 16, 1);
    expect(isLegal(lastReport())).toBe(true);

    aspect.value = "Free";
    aspect.dispatchEvent(new Event("change"));
    drag(stage(), at(0, 0), at(1, 0.5));
    expect(lastReport()).toEqual({ x: 0, y: 0, w: 3840, h: 1080 });
  });

  it("moves with the arrow keys and resizes with Shift", () => {
    mount();
    drag(stage(), at(0.25, 0.25), at(0.5, 0.5));

    key("ArrowRight");
    expect(lastReport()).toEqual({ x: 962, y: 540, w: 960, h: 540 });
    key("ArrowDown", { altKey: true });
    expect(lastReport()).toEqual({ x: 962, y: 560, w: 960, h: 540 });
    key("ArrowLeft", { shiftKey: true });
    expect(lastReport()).toEqual({ x: 962, y: 560, w: 958, h: 540 });
    key("ArrowUp", { shiftKey: true });
    expect(lastReport()).toEqual({ x: 962, y: 560, w: 958, h: 538 });

    const event = key("q");
    expect(event.defaultPrevented).toBe(false);
  });

  it("puts the whole frame back with Reset or Escape", () => {
    mount();
    drag(stage(), at(0.25, 0.25), at(0.5, 0.5));

    host.querySelector(".crop-reset").dispatchEvent(new Event("click"));
    expect(lastReport()).toEqual({ x: 0, y: 0, w: 3840, h: 2160 });

    drag(stage(), at(0.25, 0.25), at(0.5, 0.5));
    key("Escape");
    expect(lastReport()).toEqual({ x: 0, y: 0, w: 3840, h: 2160 });
  });

  it("pauses a playing video, since the box is drawn on a still", () => {
    mount();

    drag(stage(), at(0.25, 0.25), at(0.5, 0.5));

    expect(video.pause).toHaveBeenCalled();
  });

  it("keeps a drawn box inside a frame that turns out smaller", () => {
    mount({}, { videoWidth: 3840, videoHeight: 2160 });
    drag(stage(), at(0.5, 0.5), at(1, 1));
    onChange.mockClear();

    video.setSize(1280, 720);

    expect(lastReport()).toEqual({ x: 0, y: 0, w: 1280, h: 720 });
  });

  it("ignores presses that are not the main button, or on a stage with no size", () => {
    mount();
    const right = new MouseEvent("pointerdown", { bubbles: true, button: 2, clientX: 200 });
    stage().dispatchEvent(right);
    pointer(document, "pointermove", at(0.9, 0.9));

    stage().getBoundingClientRect = () => ({ left: 0, top: 0, width: 0, height: 0 });
    drag(stage(), [0, 0], [10, 10]);

    expect(onChange).not.toHaveBeenCalled();
  });

  it("draws the paused frame as a small still, never a 4K canvas", () => {
    mount();
    drawImage.mockClear();

    video.dispatchEvent(new Event("pause"));

    const canvas = host.querySelector(".crop-frame");
    expect(drawImage).toHaveBeenCalledWith(video, 0, 0, 640, 360);
    expect([canvas.width, canvas.height]).toEqual([640, 360]);
  });

  it("asks an unplayed video for one frame to draw, and only once", () => {
    const element = fakeVideo();
    Object.defineProperty(element, "readyState", { configurable: true, get: () => 1 });
    const seeks = [];
    Object.defineProperty(element, "currentTime", {
      configurable: true,
      get: () => 0,
      set: (value) => seeks.push(value),
    });
    document.body.append(element);

    cleanup = window.mountCropBox(element, onChange, { ...SOURCE, host });
    element.dispatchEvent(new Event("loadedmetadata"));

    expect(seeks).toEqual([0.01]);
  });

  it("does not seek a video that already has a frame, or cannot seek yet", () => {
    mount();
    expect(drawImage).toHaveBeenCalled();

    const element = fakeVideo();
    Object.defineProperty(element, "readyState", { configurable: true, get: () => 0 });
    Object.defineProperty(element, "currentTime", {
      configurable: true,
      get: () => 0,
      set: () => {
        throw new DOMException("not yet", "InvalidStateError");
      },
    });
    expect(() => window.mountCropBox(element, onChange, { ...SOURCE, host })()).not.toThrow();
  });

  it("still works on a plain stage when the frame cannot be drawn", () => {
    drawImage.mockImplementation(() => {
      throw new DOMException("tainted", "SecurityError");
    });
    HTMLCanvasElement.prototype.getContext.mockImplementationOnce(() => {
      throw new Error("no canvas");
    });
    mount();
    video.dispatchEvent(new Event("seeked"));

    drag(stage(), at(0, 0), at(0.5, 0.5));

    expect(lastReport()).toEqual({ x: 0, y: 0, w: 1920, h: 1080 });
  });

  it("takes a typed size, centred on the box it had, and switches to Free", () => {
    mount();
    const [width, height] = host.querySelectorAll(".crop-size-input");
    const aspect = host.querySelector(".crop-aspect");
    aspect.value = "16:9";
    aspect.dispatchEvent(new Event("change"));

    width.value = "1000";
    height.value = "1000";
    height.dispatchEvent(new Event("change"));

    expect(lastReport()).toEqual({ x: 1420, y: 580, w: 1000, h: 1000 });
    expect(aspect.value).toBe("Free");
    expect(readout()).toBe("1000 × 1000 from 1420, 580 of 3840 × 2160");
  });

  it("makes a typed size legal and shows what it became", () => {
    mount();
    const [width, height] = host.querySelectorAll(".crop-size-input");

    width.value = "5001";
    height.value = "999";
    width.dispatchEvent(new KeyboardEvent("keydown", { key: "Enter", bubbles: true }));

    expect(lastReport()).toEqual({ x: 0, y: 580, w: 3840, h: 998 });
    expect(isLegal(lastReport())).toBe(true);
    expect([width.value, height.value]).toEqual(["3840", "998"]);
  });

  it("keeps the typed fields in step with a drawn box", () => {
    mount();
    drag(stage(), at(0.25, 0.25), at(0.75, 0.75));
    const [width, height] = host.querySelectorAll(".crop-size-input");
    expect([width.value, height.value]).toEqual([String(lastReport().w), String(lastReport().h)]);
  });

  it("ignores an empty or nonsense size and puts the real one back", () => {
    mount();
    drag(stage(), at(0.25, 0.25), at(0.75, 0.75));
    const before = lastReport();
    const calls = onChange.mock.calls.length;
    const [width] = host.querySelectorAll(".crop-size-input");

    width.value = "";
    width.dispatchEvent(new Event("change"));

    expect(onChange.mock.calls.length).toBe(calls);
    expect(width.value).toBe(String(before.w));
  });

  it("stops listening and removes itself when unmounted", () => {
    mount();
    pointer(stage(), "pointerdown", at(0.25, 0.25));

    cleanup();
    cleanup = null;
    pointer(document, "pointermove", at(0.75, 0.75));
    video.setSize(100, 100);

    expect(onChange).not.toHaveBeenCalled();
    expect(host.querySelector(".crop-box")).toBeNull();
  });
});
