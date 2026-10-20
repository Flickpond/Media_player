// @vitest-environment jsdom
//
// Tests for preset.js, the panel the editor mounts through
// `window.mountPresetPanel(video, onChange, frame)`. jsdom has no WebGL, so
// the context below is a small stand-in: enough surface to prove the real
// component compiles one shader pair, paints the video as a texture, changes
// the preset uniform, and falls back cleanly when a browser cannot do any of
// that.

import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import "./preset.js";

const NAMES = ["bw", "warm", "cool", "vintage", "vivid"];

let host;
let video;
let onChange;
let cleanup;
let gl;
let frames;
let nextFrame;
let images;
let loadImages;

/** A WebGL context with only the calls preset.js makes. */
function fakeGl({ compile = true, link = true, textureThrows = false, shader = true } = {}) {
  const context = {
    VERTEX_SHADER: 1,
    FRAGMENT_SHADER: 2,
    COMPILE_STATUS: 3,
    LINK_STATUS: 4,
    ARRAY_BUFFER: 5,
    STATIC_DRAW: 6,
    FLOAT: 7,
    TRIANGLE_STRIP: 8,
    TEXTURE_2D: 9,
    RGBA: 10,
    UNSIGNED_BYTE: 11,
    TEXTURE0: 12,
    TEXTURE1: 20,
    LINEAR: 13,
    CLAMP_TO_EDGE: 14,
    TEXTURE_MIN_FILTER: 15,
    TEXTURE_MAG_FILTER: 16,
    TEXTURE_WRAP_S: 17,
    TEXTURE_WRAP_T: 18,
    UNPACK_FLIP_Y_WEBGL: 19,
    createShader: vi.fn(() => (shader ? {} : null)),
    shaderSource: vi.fn(),
    compileShader: vi.fn(),
    getShaderParameter: vi.fn(() => compile),
    deleteShader: vi.fn(),
    createProgram: vi.fn(() => ({})),
    attachShader: vi.fn(),
    linkProgram: vi.fn(),
    getProgramParameter: vi.fn(() => link),
    deleteProgram: vi.fn(),
    useProgram: vi.fn(),
    createBuffer: vi.fn(() => ({})),
    bindBuffer: vi.fn(),
    bufferData: vi.fn(),
    getAttribLocation: vi.fn(() => 0),
    enableVertexAttribArray: vi.fn(),
    vertexAttribPointer: vi.fn(),
    activeTexture: vi.fn(),
    createTexture: vi.fn(() => ({})),
    bindTexture: vi.fn(),
    texParameteri: vi.fn(),
    pixelStorei: vi.fn(),
    getUniformLocation: vi.fn((_program, name) => ({ name })),
    uniform1i: vi.fn(),
    viewport: vi.fn(),
    texImage2D: vi.fn(() => {
      if (textureThrows) throw new Error("SecurityError");
    }),
    drawArrays: vi.fn(),
    deleteTexture: vi.fn(),
    deleteBuffer: vi.fn(),
  };
  return context;
}

/** A <video> with the media properties preset.js reads. */
function fakeVideo({ width = 1920, height = 1080, ready = 4, paused = true } = {}) {
  const element = document.createElement("video");
  let current = { width, height, ready, paused };
  Object.defineProperty(element, "videoWidth", {
    configurable: true,
    get: () => current.width,
  });
  Object.defineProperty(element, "videoHeight", {
    configurable: true,
    get: () => current.height,
  });
  Object.defineProperty(element, "readyState", {
    configurable: true,
    get: () => current.ready,
  });
  Object.defineProperty(element, "paused", {
    configurable: true,
    get: () => current.paused,
  });
  Object.defineProperty(element, "ended", { configurable: true, get: () => false });
  element.set = (values) => {
    current = { ...current, ...values };
  };
  return element;
}

function mount(options = {}) {
  cleanup = window.mountPresetPanel(video, onChange, options);
}

const tiles = () => [...host.querySelectorAll(".preset-tile")];
const tile = (name) => host.querySelector(`.preset-tile[data-preset="${name}"]`);
const preview = () => video.parentElement.querySelector(".preset-preview");
const clear = () => host.querySelector(".preset-clear");
const checkbox = () => host.querySelector('summary input[type="checkbox"]');
const lastReport = () => onChange.mock.calls.at(-1)?.[0];

function flushFrames() {
  const pending = [...frames.values()];
  frames.clear();
  for (const callback of pending) callback();
}

function key(name) {
  const event = new KeyboardEvent("keydown", { key: name, bubbles: true, cancelable: true });
  document.activeElement?.dispatchEvent(event);
  return event;
}

beforeEach(() => {
  document.body.innerHTML = "";
  host = document.createElement("details");
  host.dataset.operation = "preset";
  host.dataset.mount = "mountPresetPanel";
  host.innerHTML =
    '<summary><input type="checkbox" checked /></summary><div class="edit-mount"></div>';
  document.body.append(host);

  video = fakeVideo();
  document.body.append(video);
  onChange = vi.fn();
  cleanup = null;
  gl = fakeGl();
  frames = new Map();
  nextFrame = 0;
  images = [];
  loadImages = true;
  vi.stubGlobal("Image", class {
    naturalWidth = 1024;
    naturalHeight = 160;
    constructor() { images.push(this); }
    set src(value) {
      this.url = value;
      if (loadImages) this.onload?.();
    }
  });
  vi.spyOn(HTMLCanvasElement.prototype, "getContext").mockImplementation((type) =>
    type === "webgl" ? gl : null,
  );
  vi.stubGlobal("requestAnimationFrame", (callback) => {
    const id = ++nextFrame;
    frames.set(id, callback);
    return id;
  });
  vi.stubGlobal("cancelAnimationFrame", vi.fn((id) => frames.delete(id)));
});

afterEach(() => {
  if (cleanup) cleanup();
  vi.unstubAllGlobals();
  vi.restoreAllMocks();
  document.body.innerHTML = "";
});

describe("preset panel", () => {
  it("is the global the editor looks up", () => {
    expect(typeof window.mountPresetPanel).toBe("function");
  });

  it("offers the five agreed looks and reports nothing until one is chosen", () => {
    mount();

    expect(tiles().map((node) => node.dataset.preset)).toEqual(NAMES);
    expect(tiles()[0].getAttribute("aria-checked")).toBe("false");
    expect(clear().disabled).toBe(true);
    expect(onChange).not.toHaveBeenCalled();
  });

  it("reports the chosen name and paints the current video frame", () => {
    mount();

    tile("warm").click();
    flushFrames();

    expect(lastReport()).toEqual({ name: "warm" });
    expect(tile("warm").getAttribute("aria-checked")).toBe("true");
    expect(tile("warm").classList.contains("is-selected")).toBe(true);
    expect(clear().disabled).toBe(false);
    expect(preview().hidden).toBe(false);
    expect(gl.texImage2D).toHaveBeenCalledTimes(2); // colour table and video frame
    expect(gl.drawArrays).toHaveBeenCalledWith(gl.TRIANGLE_STRIP, 0, 4);
    expect(gl.uniform1i.mock.calls.at(-1)[1]).toBe(1);
  });

  it("switches looks without compiling the shader again", () => {
    mount();

    tile("bw").click();
    tile("vivid").click();
    flushFrames();

    expect(gl.createProgram).toHaveBeenCalledOnce();
    expect(gl.uniform1i.mock.calls.at(-1)[1]).toBe(4);
    expect(lastReport()).toEqual({ name: "vivid" });
  });

  it("caps a 4K frame at 1280 pixels wide for the preview texture", () => {
    video.set({ width: 3840, height: 2160 });
    mount();

    tile("cool").click();
    flushFrames();

    expect(preview().width).toBe(1280);
    expect(preview().height).toBe(720);
    expect(gl.viewport).toHaveBeenCalledWith(0, 0, 1280, 720);
  });

  it("waits for a decoded frame and draws when one arrives", () => {
    video.set({ ready: 0 });
    mount();

    tile("warm").click();
    flushFrames();

    expect(preview().hidden).toBe(true);
    expect(gl.drawArrays).not.toHaveBeenCalled();

    video.set({ ready: 4 });
    video.dispatchEvent(new Event("loadeddata"));
    flushFrames();

    expect(preview().hidden).toBe(false);
    expect(gl.drawArrays).toHaveBeenCalledOnce();
  });

  it("keeps drawing while the video plays", () => {
    video.set({ paused: false });
    mount();

    tile("bw").click();
    flushFrames();

    expect(gl.drawArrays).toHaveBeenCalledOnce();
    expect(frames.size).toBe(1);
  });

  it("moves the selection with the arrow keys and Home/End", () => {
    mount();
    tiles()[0].focus();

    const right = key("ArrowRight");
    expect(right.defaultPrevented).toBe(true);
    expect(lastReport()).toEqual({ name: "warm" });
    expect(document.activeElement).toBe(tile("warm"));

    key("End");
    expect(lastReport()).toEqual({ name: "vivid" });
    expect(document.activeElement).toBe(tile("vivid"));

    key("ArrowRight");
    expect(lastReport()).toEqual({ name: "bw" });

    key("Home");
    expect(document.activeElement).toBe(tile("bw"));
  });

  it("ignores keys that are not part of the radio group", () => {
    mount();
    tiles()[0].focus();

    const event = key("a");

    expect(event.defaultPrevented).toBe(false);
    expect(onChange).not.toHaveBeenCalled();
  });

  it("returns to the original and restores the video's previous filter", () => {
    video.style.filter = "brightness(1.1)";
    mount();
    tile("vintage").click();
    flushFrames();

    clear().click();

    expect(lastReport()).toBeNull();
    expect(preview().hidden).toBe(true);
    expect(clear().disabled).toBe(true);
    expect(video.style.filter).toBe("brightness(1.1)");
  });

  it("hides the look when Presets is unticked, and shows it again when re-ticked", () => {
    video.style.filter = "brightness(1.1)";
    checkbox().checked = true;
    mount();
    tile("warm").click();
    flushFrames();
    expect(preview().hidden).toBe(false);

    checkbox().checked = false;
    checkbox().dispatchEvent(new Event("change"));
    expect(preview().hidden).toBe(true);
    expect(video.style.filter).toBe("brightness(1.1)");

    checkbox().checked = true;
    checkbox().dispatchEvent(new Event("change"));
    flushFrames();
    expect(preview().hidden).toBe(false);
  });

  it("falls back to a CSS preview when WebGL is unavailable", () => {
    HTMLCanvasElement.prototype.getContext.mockReturnValue(null);
    mount();

    tile("cool").click();

    expect(preview()).toBeNull();
    expect(video.style.filter).toContain("hue-rotate(8deg)");
    expect(lastReport()).toEqual({ name: "cool" });
    expect(host.querySelector(".preset-note").textContent).toContain("Approximate preview");
  });

  it("does not bring an unticked preview back on playback, seek or a new frame", () => {
    mount();
    tile("warm").click();
    flushFrames();
    checkbox().checked = false;
    checkbox().dispatchEvent(new Event("change"));
    gl.drawArrays.mockClear();
    for (const type of ["play", "seeked", "loadeddata", "pause"]) {
      video.dispatchEvent(new Event(type));
      flushFrames();
    }
    expect(preview().hidden).toBe(true);
    expect(gl.drawArrays).not.toHaveBeenCalled();
    expect(video.style.filter).toBe("");
  });

  it("keeps a selection made while unticked hidden until it is enabled", () => {
    checkbox().checked = false;
    mount();
    tile("warm").click();
    flushFrames();
    expect(lastReport()).toEqual({ name: "warm" });
    expect(preview()).toBeNull();
    checkbox().checked = true;
    checkbox().dispatchEvent(new Event("change"));
    flushFrames();
    expect(preview().hidden).toBe(false);
    expect(gl.uniform1i.mock.calls.at(-1)[1]).toBe(1);
  });

  it("uses the new selection when a previously active preview is enabled again", () => {
    mount();
    tile("warm").click();
    flushFrames();
    checkbox().checked = false;
    checkbox().dispatchEvent(new Event("change"));
    tile("cool").click();
    checkbox().checked = true;
    checkbox().dispatchEvent(new Event("change"));
    flushFrames();
    expect(lastReport()).toEqual({ name: "cool" });
    expect(gl.uniform1i.mock.calls.at(-1)[1]).toBe(2);
  });

  it("keeps the chosen look visible if the GPU context is lost", () => {
    mount();
    tile("warm").click();
    flushFrames();
    preview().dispatchEvent(new Event("webglcontextlost"));
    flushFrames();
    expect(preview()).toBeNull();
    expect(video.style.filter).toContain("sepia");
    expect(lastReport()).toEqual({ name: "warm" });
  });

  it("draws a paused frame when the colour tables finish loading", () => {
    loadImages = false;
    mount();
    tile("cool").click();
    flushFrames();
    expect(preview().hidden).toBe(true);
    images[0].onload();
    flushFrames();
    expect(preview().hidden).toBe(false);
    expect(images[0].url).toContain("preset-luts.png");
  });

  it.each(["error", "size"])("uses the approximate preview on a colour-table %s", (failure) => {
    loadImages = false;
    mount();
    tile("warm").click();
    if (failure === "error") images[0].onerror();
    else {
      images[0].naturalWidth = 1;
      images[0].onload();
    }
    flushFrames();
    expect(preview()).toBeNull();
    expect(video.style.filter).toContain("sepia");
    expect(host.querySelector(".preset-note").textContent).toContain("Approximate preview");
  });

  it("cannot resurrect a closed editor when a colour-table request finishes", () => {
    loadImages = false;
    mount();
    tile("warm").click();
    const loaded = images[0].onload;
    const failed = images[0].onerror;
    cleanup();
    cleanup = null;
    gl.texImage2D.mockClear();
    loaded();
    failed();
    flushFrames();
    expect(preview()).toBeNull();
    expect(gl.texImage2D).not.toHaveBeenCalled();
  });

  it("falls back when the shader cannot be created", () => {
    gl.createShader.mockReturnValue(null);
    mount();

    tile("warm").click();

    expect(preview()).toBeNull();
    expect(video.style.filter).toContain("sepia");
  });

  it("falls back when the shader does not compile", () => {
    gl.getShaderParameter.mockReturnValue(false);
    mount();

    tile("warm").click();

    expect(preview()).toBeNull();
    expect(gl.deleteShader).toHaveBeenCalled();
    expect(video.style.filter).toContain("sepia");
  });

  it("falls back when the program does not link", () => {
    gl.getProgramParameter.mockReturnValue(false);
    mount();

    tile("vivid").click();

    expect(preview()).toBeNull();
    expect(gl.deleteProgram).toHaveBeenCalled();
    expect(video.style.filter).toContain("saturate(1.38)");
  });

  it("falls back when setting up the WebGL buffers throws", () => {
    gl.createBuffer.mockImplementation(() => {
      throw new Error("context lost");
    });
    mount();

    tile("cool").click();

    expect(preview()).toBeNull();
    expect(video.style.filter).toContain("hue-rotate(8deg)");
  });

  it("falls back when the storage origin refuses the texture", () => {
    gl = fakeGl({ textureThrows: true });
    HTMLCanvasElement.prototype.getContext.mockReturnValue(gl);
    mount();

    tile("bw").click();
    flushFrames();

    expect(preview()).toBeNull();
    expect(video.style.filter).toBe("grayscale(1)");
    expect(gl.deleteProgram).toHaveBeenCalled();
  });

  it("cleans up its canvas, listeners and frames when the editor closes", () => {
    mount();
    tile("warm").click();
    flushFrames();
    const canvas = preview();
    // A seek/pause is what schedules one more frame in the real player; the
    // close must cancel that pending request as well as remove the canvas.
    video.dispatchEvent(new Event("pause"));

    cleanup();
    cleanup = null;
    gl.drawArrays.mockClear();
    video.dispatchEvent(new Event("loadeddata"));
    flushFrames();

    expect(canvas.isConnected).toBe(false);
    expect(gl.deleteProgram).toHaveBeenCalled();
    expect(gl.drawArrays).not.toHaveBeenCalled();
    expect(host.querySelector(".preset-panel")).toBeNull();
    expect(window.cancelAnimationFrame).toHaveBeenCalled();
  });

  it("draws into a host of its own when the edit section is absent", () => {
    host.remove();
    const player = document.createElement("div");
    player.className = "player-host";
    player.append(video);
    document.body.append(player);

    mount();

    expect(player.nextElementSibling.className).toBe("preset-host");
    expect(player.nextElementSibling.querySelector(".preset-panel")).not.toBeNull();
  });
});
