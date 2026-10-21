// @vitest-environment jsdom
import { beforeEach, afterEach, expect, it, vi } from "vitest";
import "./motion.js";

let video, cleanups;
const section = (name) => document.querySelector(`[data-operation="${name}"]`);
const checkbox = (name, checked) => {
  const input = section(name).querySelector("input");
  input.checked = checked;
  input.dispatchEvent(new Event("change"));
};

beforeEach(() => {
  document.body.innerHTML = ["rotate", "flip", "speed"].map((name) =>
    `<details data-operation="${name}"><input type="checkbox"><div class="edit-mount"></div></details>`,
  ).join("");
  video = document.createElement("video");
  document.body.append(video);
  video.style.transform = "translateX(1px)";
  video.style.filter = "brightness(1.1)";
  Object.defineProperties(video, {
    videoWidth: { configurable: true, value: 1920 },
    videoHeight: { configurable: true, value: 1080 },
    clientWidth: { configurable: true, value: 400 },
    clientHeight: { configurable: true, value: 225 },
  });
  cleanups = [];
});
afterEach(() => { cleanups.reverse().forEach((fn) => fn()); vi.unstubAllGlobals(); });

it("reports clockwise choices using native keyboard-accessible buttons and previews only when enabled", () => {
  const report = vi.fn();
  cleanups.push(window.mountRotatePanel(video, report));
  expect(report).toHaveBeenLastCalledWith({ degrees: 90 });
  expect(video.style.transform).toBe("translateX(1px)");
  checkbox("rotate", true);
  expect(video.style.transform).toContain("scale(0.5625)");
  expect(video.style.transform).toContain("rotate(90deg)");
  const buttons = section("rotate").querySelectorAll("button");
  for (let i = 0; i < buttons.length; i++) {
    expect(buttons[i].type).toBe("button");
    buttons[i].focus();
    buttons[i].click(); // Native buttons also synthesize click on Enter/Space in browsers.
    expect(document.activeElement).toBe(buttons[i]);
    expect(report).toHaveBeenLastCalledWith({ degrees: [90, 180, 270][i] });
    expect(buttons[i].getAttribute("aria-pressed")).toBe("true");
  }
  checkbox("rotate", false);
  expect(video.style.transform).toBe("translateX(1px)");
  expect(video.style.filter).toBe("brightness(1.1)");
});

it("composes mirror after rotation and cleans either panel without clearing the other", () => {
  const rotate = window.mountRotatePanel(video, vi.fn());
  const report = vi.fn();
  const flip = window.mountFlipPanel(video, report);
  checkbox("rotate", true);
  checkbox("flip", true);
  expect(video.style.transform).toContain("scaleX(-1)");
  expect(video.style.transform).toContain("rotate(90deg)");
  section("flip").querySelectorAll("button")[1].click();
  expect(report).toHaveBeenLastCalledWith({ axis: "vertical" });
  expect(video.style.transform).toContain("scaleY(-1)");
  rotate();
  expect(video.style.transform).toContain("scaleY(-1)");
  expect(video.style.transform).toContain("rotate(0deg)");
  checkbox("flip", false);
  expect(video.style.transform).toBe("translateX(1px)");
  flip();
  expect(video.style.transform).toBe("translateX(1px)");
  // Listeners removed, and reopening starts with fresh defaults.
  checkbox("rotate", true);
  expect(video.style.transform).toBe("translateX(1px)");
  cleanups.push(window.mountRotatePanel(video, vi.fn()));
  expect(video.style.transform).toContain("rotate(90deg)");
});

it("fits portrait frames using source metadata and observes layout changes", () => {
  const disconnect = vi.fn(), observe = vi.fn();
  let resized;
  vi.stubGlobal("ResizeObserver", class {
    constructor(fn) { resized = fn; }
    observe = observe;
    disconnect = disconnect;
  });
  Object.defineProperty(video, "videoWidth", { value: 0 });
  Object.defineProperty(video, "videoHeight", { value: 0 });
  const cleanup = window.mountRotatePanel(video, vi.fn(), { width: 1080, height: 1920 });
  checkbox("rotate", true);
  expect(video.style.transform).toContain("scale(1.7777777777777777)");
  expect(observe).toHaveBeenCalledWith(video);
  resized();
  video.dispatchEvent(new Event("loadedmetadata"));
  window.dispatchEvent(new Event("resize"));
  cleanup();
  expect(disconnect).toHaveBeenCalled();
});

it("handles metadata and layout that have not arrived yet", () => {
  Object.defineProperty(video, "clientWidth", { value: 0 });
  cleanups.push(window.mountRotatePanel(video, vi.fn()));
  checkbox("rotate", true);
  expect(video.style.transform).toContain("scale(1)");
});

it("offers every quarter-step speed, preserves pitch and restores playback state", () => {
  video.playbackRate = 1.25;
  video.preservesPitch = false;
  const report = vi.fn();
  const cleanup = window.mountSpeedPanel(video, report);
  const select = section("speed").querySelector("select");
  expect(select.closest("label").textContent).toContain("Playback speed");
  expect([...select.options].map((o) => Number(o.value))).toEqual([0.5, 0.75, 1, 1.25, 1.5, 1.75, 2]);
  expect(report).toHaveBeenLastCalledWith({ factor: 1 });
  expect(video.playbackRate).toBe(1.25);
  checkbox("speed", true);
  for (const factor of [0.5, 2]) {
    select.value = String(factor);
    select.dispatchEvent(new Event("change"));
    expect(video.playbackRate).toBe(factor);
    expect(video.preservesPitch).toBe(true);
    expect(report).toHaveBeenLastCalledWith({ factor });
  }
  checkbox("speed", false);
  expect(video.playbackRate).toBe(1.25);
  expect(video.preservesPitch).toBe(false);
  checkbox("speed", true);
  cleanup();
  expect(video.playbackRate).toBe(1.25);
  expect(video.preservesPitch).toBe(false);
  checkbox("speed", true);
  expect(video.playbackRate).toBe(1.25);
  expect(section("speed").querySelector("select")).toBeNull();
});
