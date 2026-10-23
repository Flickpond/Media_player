// The colour panel: Track C's sprint 6 operation (sprint6-plan.md 2.1).
//
// `window.mountAdjustPanel(video, onChange, frame)` draws four sliders --
// brightness, contrast, saturation, gamma -- into the Colour section, and
// reports `{ brightness?, contrast?, saturation?, gamma? }` with only the
// values moved off neutral (null once all four are back). app.js sends that
// as the `adjust` operation's params; the worker turns it into FFmpeg's `eq`.
//
// The preview is the browser's, the result is FFmpeg's, and the point of this
// file is that the two agree. So the preview is not CSS's brightness() and
// contrast(), which mean something else: it is an SVG filter that does what
// `eq` does, in the same order and on the same numbers -- to Y'CbCr (BT.709),
// eq's luma curve on limited-range codes, its chroma scale around the middle,
// and back (docs/c-sprint6-colour.md has the stills it was checked against).
//
// A plain script like app.js: no build step, no imports, one global.

(function () {
  "use strict";

  /** The sliders, in eq's option order. The ranges are AdjustParams' in
   *  app/schemas/edit.py; `neutral` is eq's default, which changes nothing. */
  const SLIDERS = [
    { name: "brightness", label: "Brightness", min: -0.3, max: 0.3, neutral: 0 },
    { name: "contrast", label: "Contrast", min: 0.5, max: 2, neutral: 1 },
    { name: "saturation", label: "Saturation", min: 0, max: 3, neutral: 1 },
    { name: "gamma", label: "Gamma", min: 0.5, max: 2, neutral: 1 },
  ];
  const STEP = 0.01;
  const SVG_NS = "http://www.w3.org/2000/svg";
  /** Samples of each curve. The filter interpolates between them; 256 is one
   *  per 8-bit code, as fine as the picture itself. */
  const TABLE_SIZE = 256;

  // BT.709 R'G'B' <-> Y'CbCr on the gamma-encoded values a decoded video
  // reaches the page as. Cb and Cr carry +0.5, because filter results are
  // kept between 0 and 1.
  const TO_YCBCR = [
    "0.2126 0.7152 0.0722 0 0",
    "-0.114572 -0.385428 0.5 0 0.5",
    "0.5 -0.454153 -0.045847 0 0.5",
    "0 0 0 1 0",
  ].join(" ");
  const FROM_YCBCR = [
    "1 0 1.5748 0 -0.7874",
    "1 -0.187324 -0.468124 0 0.327724",
    "1 1.8556 0 0 -0.9278",
    "0 0 0 1 0",
  ].join(" ");

  let mounted = 0;

  function clamp(value, low, high) {
    return Math.min(Math.max(value, low), high);
  }

  function round2(value) {
    return Math.round(value * 100) / 100;
  }

  /** eq's table entry for one 8-bit code (libavfilter/vf_eq.c, create_lut),
   *  without its truncation to a whole code: the filter interpolates, so the
   *  average of that truncation, half a code, is taken off instead. */
  function eqCode(code, { brightness, contrast, gamma }) {
    let v = contrast * (code / 255 - 0.5) + 0.5 + brightness;
    if (v <= 0) return 0;
    v = Math.pow(v, 1 / gamma);
    return v >= 1 ? 255 : clamp(256 * v - 0.5, 0, 255);
  }

  /** The luma curve on full-range Y' (0..1), through the limited-range codes
   *  (16..235) eq actually sees in a video file. */
  function lumaTable(values) {
    const table = [];
    for (let i = 0; i < TABLE_SIZE; i += 1) {
      const code = 16 + (219 * i) / (TABLE_SIZE - 1);
      const out = eqCode(code, values);
      table.push(clamp((out - 16) / 219, 0, 1).toFixed(4));
    }
    return table.join(" ");
  }

  /** The chroma scale on Cb or Cr (+0.5): eq's contrast with brightness 0 and
   *  gamma 1, around the middle code, on the 16..240 chroma codes. */
  function chromaTable(saturation) {
    const table = [];
    for (let i = 0; i < TABLE_SIZE; i += 1) {
      const code = 16 + (224 * i) / (TABLE_SIZE - 1);
      const out = eqCode(code, { brightness: 0, contrast: saturation, gamma: 1 });
      table.push(clamp((out - 16) / 224, 0, 1).toFixed(4));
    }
    return table.join(" ");
  }

  function svgElement(tag, attributes) {
    const node = document.createElementNS(SVG_NS, tag);
    for (const [name, value] of Object.entries(attributes)) node.setAttribute(name, value);
    return node;
  }

  /** The filter the preview points the video at, in an otherwise empty SVG. */
  function createFilter(id) {
    const svg = svgElement("svg", {
      class: "adjust-filter-defs",
      width: "0",
      height: "0",
      "aria-hidden": "true",
      focusable: "false",
    });
    const filter = svgElement("filter", {
      id,
      x: "0",
      y: "0",
      width: "1",
      height: "1",
      // The default, linearRGB, would un-gamma the picture first: eq works
      // on the encoded values, so the filter has to as well.
      "color-interpolation-filters": "sRGB",
    });
    const luma = svgElement("feFuncR", { type: "table" });
    const cb = svgElement("feFuncG", { type: "table" });
    const cr = svgElement("feFuncB", { type: "table" });
    const transfer = svgElement("feComponentTransfer", {});
    transfer.append(luma, cb, cr);
    filter.append(
      svgElement("feColorMatrix", { type: "matrix", values: TO_YCBCR }),
      transfer,
      svgElement("feColorMatrix", { type: "matrix", values: FROM_YCBCR }),
    );
    svg.append(filter);
    document.body.append(svg);
    return {
      svg,
      update(values) {
        luma.setAttribute("tableValues", lumaTable(values));
        const chroma = chromaTable(values.saturation);
        cb.setAttribute("tableValues", chroma);
        cr.setAttribute("tableValues", chroma);
      },
    };
  }

  function format(slider, value) {
    if (slider.name !== "brightness") return value.toFixed(2);
    if (value === 0) return "0.00";
    return (value > 0 ? "+" : "−") + Math.abs(value).toFixed(2);
  }

  /** Where to draw, when the page did not say: the Colour section's mount
   *  (index.html), or right after the player as a last resort. */
  function defaultHost(video) {
    const host = document.querySelector('[data-operation="adjust"] .edit-mount');
    if (host) return host;
    const spot = document.createElement("div");
    (video.closest(".player-host") ?? video).after(spot);
    return spot;
  }

  function mountAdjustPanel(video, onChange, _frame, host = defaultHost(video)) {
    mounted += 1;
    const filterId = `flickpond-adjust-${mounted}`;
    const previousFilter = video.style.filter;
    const filter = createFilter(filterId);
    const checkbox = host.closest("details")?.querySelector('summary input[type="checkbox"]') ?? null;
    const values = Object.fromEntries(SLIDERS.map((slider) => [slider.name, slider.neutral]));

    const root = document.createElement("div");
    root.className = "adjust-panel";
    const inputs = {};
    const outputs = {};
    for (const slider of SLIDERS) {
      const id = `adjust-${slider.name}-${mounted}`;
      const row = document.createElement("div");
      row.className = "adjust-row";
      const label = document.createElement("label");
      label.htmlFor = id;
      label.textContent = slider.label;
      const output = document.createElement("output");
      output.className = "adjust-value";
      output.setAttribute("for", id);
      const input = document.createElement("input");
      Object.assign(input, {
        type: "range",
        id,
        min: String(slider.min),
        max: String(slider.max),
        step: String(STEP),
        value: String(slider.neutral),
      });
      input.dataset.adjust = slider.name;
      row.append(label, output, input);
      root.append(row);
      inputs[slider.name] = input;
      outputs[slider.name] = output;
    }
    const reset = document.createElement("button");
    reset.type = "button";
    reset.className = "btn-ghost adjust-reset";
    reset.textContent = "Reset";
    root.append(reset);
    host.append(root);

    /** Only what was moved off neutral is sent: a value left alone stays as
     *  it is, which is what leaving the field out means to the API. */
    function params() {
      const moved = {};
      for (const slider of SLIDERS) {
        if (values[slider.name] !== slider.neutral) moved[slider.name] = values[slider.name];
      }
      return Object.keys(moved).length ? moved : null;
    }

    /** The preview shows what Process would do: the picture changes while
     *  the section is ticked and something has been moved. */
    function renderPreview() {
      const active = params() !== null && (checkbox === null || checkbox.checked);
      if (active) {
        filter.update(values);
        video.style.filter = `url(#${filterId})`;
      } else {
        video.style.filter = previousFilter;
      }
    }

    function render() {
      for (const slider of SLIDERS) {
        inputs[slider.name].value = String(values[slider.name]);
        outputs[slider.name].textContent = format(slider, values[slider.name]);
      }
      reset.disabled = params() === null;
      renderPreview();
    }

    function report() {
      const selection = params();
      onChange(selection);
      // Moving a slider is asking for the change: tick the section, as a
      // reader would expect, rather than leave a preview that Process ignores.
      if (selection && checkbox && !checkbox.checked && !checkbox.disabled) {
        checkbox.checked = true;
        checkbox.dispatchEvent(new Event("change", { bubbles: true }));
      }
    }

    function onInput(event) {
      const name = event.target.dataset.adjust;
      const slider = SLIDERS.find((item) => item.name === name);
      if (!slider) return;
      values[name] = round2(clamp(Number(event.target.value), slider.min, slider.max));
      render();
      report();
    }

    function onReset() {
      for (const slider of SLIDERS) values[slider.name] = slider.neutral;
      render();
      report();
    }

    root.addEventListener("input", onInput);
    reset.addEventListener("click", onReset);
    checkbox?.addEventListener("change", renderPreview);
    render();

    return function unmountAdjustPanel() {
      checkbox?.removeEventListener("change", renderPreview);
      video.style.filter = previousFilter;
      filter.svg.remove();
      root.remove();
    };
  }

  window.mountAdjustPanel = mountAdjustPanel;
})();
