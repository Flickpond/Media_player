// @vitest-environment jsdom
//
// Tests for adjust.js, the colour panel the editor mounts through
// `window.mountAdjustPanel(video, onChange, frame)` (EDITOR-COMPONENTS.md,
// "Operations added since sprint 6"). The panel side of that seam is covered
// in app.test.js with a stand-in; these are about the component's promise:
// it reports only what was moved, in the API's ranges, and its preview is
// eq's arithmetic -- checked here on the filter's own tables, and against
// FFmpeg's output in docs/c-sprint6-colour.md.

import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import "./adjust.js";

let host;
let video;
let onChange;
let cleanup;

/** The Colour section as index.html has it, ticked or not. */
function section({ checked = false, disabled = false } = {}) {
  const details = document.createElement("details");
  details.dataset.operation = "adjust";
  details.dataset.mount = "mountAdjustPanel";
  details.innerHTML =
    '<summary><input type="checkbox" id="edit-adjust" /> Colour</summary>' +
    '<div class="edit-op-body"><div class="edit-mount"></div></div>';
  document.body.append(details);
  const box = details.querySelector("input");
  box.checked = checked;
  box.disabled = disabled;
  return { details, box, mount: details.querySelector(".edit-mount") };
}

function mount(target = host) {
  cleanup = window.mountAdjustPanel(video, onChange, { width: 1920, height: 1080, duration: 8 }, target);
  return cleanup;
}

const slider = (name) => document.querySelector(`input[data-adjust="${name}"]`);
const readout = (name) => slider(name).closest(".adjust-row").querySelector("output").textContent;
const resetButton = () => document.querySelector(".adjust-reset");
const lastReport = () => onChange.mock.calls.at(-1)[0];
const table = (tag) =>
  document
    .querySelector(`.adjust-filter-defs ${tag}`)
    .getAttribute("tableValues")
    .split(" ")
    .map(Number);

function move(name, value) {
  const input = slider(name);
  input.value = String(value);
  input.dispatchEvent(new Event("input", { bubbles: true }));
}

beforeEach(() => {
  document.body.innerHTML = "";
  video = document.createElement("video");
  document.body.append(video);
  host = document.createElement("div");
  document.body.append(host);
  onChange = vi.fn();
  cleanup = null;
});

afterEach(() => {
  if (cleanup) cleanup();
  document.body.innerHTML = "";
});

describe("colour panel", () => {
  it("is the global the editor looks up", () => {
    expect(typeof window.mountAdjustPanel).toBe("function");
  });

  it("draws four labelled sliders in the API's ranges, at neutral", () => {
    mount();

    const expected = {
      brightness: ["-0.3", "0.3", "0"],
      contrast: ["0.5", "2", "1"],
      saturation: ["0", "3", "1"],
      gamma: ["0.5", "2", "1"],
    };
    for (const [name, [min, max, value]] of Object.entries(expected)) {
      const input = slider(name);
      expect(input.type).toBe("range");
      expect([input.min, input.max, input.value, input.step]).toEqual([min, max, value, "0.01"]);
      const label = document.querySelector(`label[for="${input.id}"]`);
      expect(label.textContent.toLowerCase()).toBe(name);
    }
    expect(readout("brightness")).toBe("0.00");
    expect(readout("contrast")).toBe("1.00");
    expect(resetButton().disabled).toBe(true);
  });

  it("reports nothing and leaves the picture alone until a slider moves", () => {
    video.style.filter = "blur(1px)";
    mount();

    expect(onChange).not.toHaveBeenCalled();
    expect(video.style.filter).toBe("blur(1px)");
  });

  it("reports only the values moved off neutral", () => {
    mount();

    move("brightness", 0.1);
    expect(lastReport()).toEqual({ brightness: 0.1 });
    expect(readout("brightness")).toBe("+0.10");

    move("saturation", 1.45);
    expect(lastReport()).toEqual({ brightness: 0.1, saturation: 1.45 });
    expect(readout("saturation")).toBe("1.45");

    move("brightness", -0.25);
    expect(readout("brightness")).toBe("−0.25");
    move("brightness", 0);
    expect(lastReport()).toEqual({ saturation: 1.45 });
    expect(resetButton().disabled).toBe(false);
  });

  it("reports null once everything is back at neutral", () => {
    mount();
    move("gamma", 1.3);

    move("gamma", 1);

    expect(lastReport()).toBeNull();
    expect(resetButton().disabled).toBe(true);
  });

  it("puts everything back with Reset", () => {
    const { mount: target } = section({ checked: true });
    mount(target);
    move("contrast", 1.5);
    move("gamma", 0.8);

    resetButton().dispatchEvent(new MouseEvent("click", { bubbles: true }));

    expect(lastReport()).toBeNull();
    expect(slider("contrast").value).toBe("1");
    expect(slider("gamma").value).toBe("1");
    expect(readout("gamma")).toBe("1.00");
    expect(video.style.filter).toBe("");
    expect(resetButton().disabled).toBe(true);
  });

  it("previews on the video while its section is ticked", () => {
    const { box, mount: target } = section({ checked: true });
    mount(target);

    move("contrast", 1.4);

    const id = video.style.filter.match(/^url\("?#([\w-]+)"?\)$/)[1];
    const filter = document.getElementById(id);
    expect(filter.tagName.toLowerCase()).toBe("filter");
    expect(filter.getAttribute("color-interpolation-filters")).toBe("sRGB");
    expect(filter.querySelectorAll("feColorMatrix")).toHaveLength(2);

    box.checked = false;
    box.dispatchEvent(new Event("change"));
    expect(video.style.filter).toBe("");

    box.checked = true;
    box.dispatchEvent(new Event("change"));
    expect(video.style.filter).toContain(id);
  });

  it("ticks its section when a slider moves, so the preview is what Process sends", () => {
    const { box, details, mount: target } = section();
    const changed = vi.fn();
    details.addEventListener("change", changed);
    mount(target);

    move("saturation", 0.5);

    expect(box.checked).toBe(true);
    expect(changed).toHaveBeenCalledTimes(1);
    expect(video.style.filter).toContain("url(");

    move("saturation", 0.6);
    expect(changed).toHaveBeenCalledTimes(1);
  });

  it("leaves a disabled section unticked", () => {
    const { box, mount: target } = section({ disabled: true });
    mount(target);

    move("gamma", 1.5);

    expect(box.checked).toBe(false);
    expect(video.style.filter).toBe("");
  });

  it("previews whenever something is moved on a page with no section", () => {
    mount();

    move("brightness", 0.2);

    expect(video.style.filter).toContain("url(");
  });

  it("ignores input that is not from one of its sliders", () => {
    mount();

    resetButton().dispatchEvent(new Event("input", { bubbles: true }));

    expect(onChange).not.toHaveBeenCalled();
  });

  describe("the preview's arithmetic", () => {
    // Full-range Y' in, full-range Y' out, through eq on limited-range codes.
    const at = (values, i) => values[i];

    it("leaves luma alone when only the colour moves", () => {
      mount();
      move("saturation", 1.01);

      const luma = table("feFuncR");
      expect(luma).toHaveLength(256);
      for (const i of [0, 64, 128, 192, 255]) expect(at(luma, i)).toBeCloseTo(i / 255, 2);
    });

    it("adds brightness on eq's 0..1 scale of 8-bit codes", () => {
      mount();
      move("brightness", 0.1);

      const luma = table("feFuncR");
      // +0.1 is 25.5 codes; on the 219 codes from black to white, 0.116.
      expect(at(luma, 128) - 128 / 255).toBeCloseTo(25.5 / 219, 2);
      expect(at(luma, 255)).toBe(1);
    });

    it("pivots contrast on the middle code", () => {
      mount();
      move("contrast", 2);

      const luma = table("feFuncR");
      expect(at(luma, 0)).toBe(0);
      expect(at(luma, 255)).toBe(1);
      // 0.5 on eq's scale is code 127.5: the middle of 16..235 barely moves.
      expect(Math.abs(at(luma, 127) - 127 / 255)).toBeLessThan(0.03);
      expect(at(luma, 160)).toBeGreaterThan(160 / 255 + 0.1);
    });

    it("raises the mid-tones with gamma above one and lowers them below", () => {
      mount();
      move("gamma", 2);
      const raised = at(table("feFuncR"), 128);
      move("gamma", 0.5);
      const lowered = at(table("feFuncR"), 128);

      expect(raised).toBeGreaterThan(0.6);
      expect(lowered).toBeLessThan(0.35);
    });

    it("scales both chroma channels around neutral by the saturation", () => {
      mount();
      move("saturation", 0);

      const cb = table("feFuncG");
      const cr = table("feFuncB");
      expect(cb).toEqual(cr);
      // No colour left: every Cb and Cr lands on the middle.
      for (const i of [0, 128, 255]) expect(at(cb, i)).toBeCloseTo(0.5, 2);

      move("saturation", 2);
      const doubled = table("feFuncG");
      expect(at(doubled, 192) - 0.5).toBeCloseTo(2 * (192 / 255 - 0.5), 1);
      expect(at(doubled, 255)).toBe(1);
    });
  });

  describe("where it draws", () => {
    it("draws into the Colour section by default", () => {
      const { mount: target } = section();
      cleanup = window.mountAdjustPanel(video, onChange, {});

      expect(target.querySelector(".adjust-panel")).not.toBeNull();
    });

    it("draws right after the player when the page has no Colour section", () => {
      const player = document.createElement("div");
      player.className = "player-host";
      player.append(video);
      document.body.append(player);

      cleanup = window.mountAdjustPanel(video, onChange, {});

      expect(player.nextElementSibling.querySelector(".adjust-panel")).not.toBeNull();
    });

    it("draws right after a bare video as a last resort", () => {
      cleanup = window.mountAdjustPanel(video, onChange, {});

      expect(video.nextElementSibling.querySelector(".adjust-panel")).not.toBeNull();
    });
  });

  it("removes itself and its filter, and gives the video its own filter back", () => {
    video.style.filter = "blur(1px)";
    const { box, mount: target } = section({ checked: true });
    mount(target);
    move("contrast", 1.5);
    expect(video.style.filter).toContain("url(");

    cleanup();
    cleanup = null;

    expect(video.style.filter).toBe("blur(1px)");
    expect(document.querySelector(".adjust-panel")).toBeNull();
    expect(document.querySelector(".adjust-filter-defs")).toBeNull();
    box.dispatchEvent(new Event("change"));
    expect(video.style.filter).toBe("blur(1px)");
  });

  it("gives each editor session a filter of its own", () => {
    mount();
    move("gamma", 1.5);
    const first = video.style.filter;
    cleanup();

    mount();
    move("gamma", 1.5);

    expect(video.style.filter).not.toBe(first);
    expect(document.querySelectorAll(".adjust-filter-defs")).toHaveLength(1);
  });
});
