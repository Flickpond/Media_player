// The crop box: the other half of the editor's component seam.
//
// The editing panel in app.js looks up `window.mountCropBox(video, onChange,
// options)` when it opens (EDITOR-COMPONENTS.md) and sends whatever this
// reports as the crop operation's params. So everything here is about one
// promise: the `{ x, y, w, h }` handed to onChange is the rectangle the reader
// drew, in pixels of the original upload, whole numbers, wholly inside its
// frame -- what the worker's validate_crop accepts -- and even, because the
// H.264 encoder behind it refuses an odd-sized 4:2:0 picture.
//
// It is drawn on a still of the paused frame in the panel rather than over the
// player itself: the player's own controls sit where a box would go, and a
// still is what "crop" means anyway -- the same box applies to every frame.
//
// A plain script like app.js: no build step, no imports, one global.

(function () {
  "use strict";

  /** The smallest box, in source pixels: below this a crop is a mistake. */
  const MIN_SIZE = 32;
  /** Arrow keys move the box this many source pixels; Shift ten times that. */
  const KEY_STEP = 2;

  const ASPECTS = [
    { label: "Free", value: null },
    { label: "16:9", value: 16 / 9 },
    { label: "4:3", value: 4 / 3 },
    { label: "1:1", value: 1 },
    { label: "9:16", value: 9 / 16 },
  ];

  function clamp(value, low, high) {
    return Math.min(Math.max(value, low), high);
  }

  /** Down to an even number, never below 2. x and y are snapped too, so the
   *  box starts on a chroma sample as well as being an even size. */
  function even(value) {
    return Math.max(2, Math.floor(value / 2) * 2);
  }

  function evenOffset(value) {
    return Math.max(0, Math.floor(value / 2) * 2);
  }

  function defaultHost(video) {
    const host = document.getElementById("edit-crop-mount");
    if (host) return host;
    const spot = document.createElement("div");
    const anchor = video.closest(".player-host") ?? video;
    anchor.after(spot);
    return spot;
  }

  function createElement(tag, className, attributes = {}) {
    const node = document.createElement(tag);
    if (className) node.className = className;
    for (const [name, value] of Object.entries(attributes)) node.setAttribute(name, value);
    return node;
  }

  /**
   * `options.width` / `options.height` are the original's real size. The
   * element's own `videoWidth` is only the rendition playing right now -- an
   * adaptive ladder can start a 4K upload at 360p -- so it is the fallback,
   * not the measure.
   */
  function mountCropBox(video, onChange, options = {}) {
    const host = options.host ?? defaultHost(video);
    const root = createElement("div", "crop-box");
    const stage = createElement("div", "crop-stage");
    const canvas = createElement("canvas", "crop-frame", { "aria-hidden": "true" });
    const selection = createElement("div", "crop-selection", {
      role: "group",
      tabindex: "0",
      "aria-label": "Crop box. Arrow keys move it, Shift+arrows resize it.",
    });
    const corners = ["nw", "ne", "sw", "se"].map((corner) => {
      const handle = createElement("div", `crop-handle crop-handle-${corner}`, {
        "aria-hidden": "true",
      });
      handle.dataset.corner = corner;
      selection.append(handle);
      return handle;
    });
    const toolbar = createElement("div", "crop-toolbar");
    const aspect = createElement("select", "crop-aspect", { "aria-label": "Aspect ratio" });
    for (const { label } of ASPECTS) {
      const option = document.createElement("option");
      option.value = label;
      option.textContent = label;
      aspect.append(option);
    }
    const reset = createElement("button", "btn-ghost crop-reset", { type: "button" });
    reset.textContent = "Reset";
    const readout = createElement("p", "crop-readout", { "aria-live": "polite" });
    toolbar.append(aspect, reset);
    stage.append(canvas, selection);
    root.append(stage, toolbar, readout);
    host.append(root);

    const state = {
      width: 0,
      height: 0,
      box: null,
      ratio: null,
      /** Until the reader draws, there is no selection to report: the panel
       *  keeps Process disabled for a ticked Crop with no box. */
      touched: false,
      drag: null,
    };

    function frameSize() {
      const width = Number(options.width) || video.videoWidth || 0;
      const height = Number(options.height) || video.videoHeight || 0;
      return width > 0 && height > 0 ? { width, height } : null;
    }

    function wholeFrame() {
      return { x: 0, y: 0, w: state.width, h: state.height };
    }

    function render() {
      const ready = state.box !== null;
      root.classList.toggle("is-disabled", !ready);
      selection.hidden = !ready;
      if (!ready) {
        readout.textContent = "Loading the video's frame…";
        return;
      }
      const { x, y, w, h } = state.box;
      stage.style.aspectRatio = `${state.width} / ${state.height}`;
      selection.style.left = `${(x / state.width) * 100}%`;
      selection.style.top = `${(y / state.height) * 100}%`;
      selection.style.width = `${(w / state.width) * 100}%`;
      selection.style.height = `${(h / state.height) * 100}%`;
      readout.textContent = state.touched
        ? `${w} × ${h} from ${x}, ${y} of ${state.width} × ${state.height}`
        : `Drag on the frame to draw a box (${state.width} × ${state.height}).`;
    }

    /** Make any requested box a legal one: inside the frame, at least the
     *  minimum, even-sized, and the chosen shape when there is one. */
    function legal(box, anchor = null) {
      const minW = Math.min(MIN_SIZE, state.width);
      const minH = Math.min(MIN_SIZE, state.height);
      let w = clamp(box.w, minW, state.width);
      let h = clamp(box.h, minH, state.height);
      if (state.ratio) {
        // Keep the shape by shrinking whichever side is too long for it.
        if (w / h > state.ratio) w = h * state.ratio;
        else h = w / state.ratio;
        if (w > state.width) {
          w = state.width;
          h = w / state.ratio;
        }
        if (h > state.height) {
          h = state.height;
          w = h * state.ratio;
        }
      }
      w = Math.min(even(w), state.width - (state.width % 2));
      h = Math.min(even(h), state.height - (state.height % 2));
      // A resize keeps the corner opposite the one being dragged where it was.
      let x = anchor?.right !== undefined ? anchor.right - w : box.x;
      let y = anchor?.bottom !== undefined ? anchor.bottom - h : box.y;
      x = evenOffset(clamp(x, 0, state.width - w));
      y = evenOffset(clamp(y, 0, state.height - h));
      return { x, y, w, h };
    }

    function setBox(box, anchor) {
      const next = legal(box, anchor);
      const previous = state.box;
      const changed =
        !state.touched ||
        !previous ||
        ["x", "y", "w", "h"].some((key) => next[key] !== previous[key]);
      state.box = next;
      state.touched = true;
      render();
      if (changed) onChange({ ...next });
    }

    function drawFrame() {
      if (!state.width) return;
      // The still is drawn at a modest size: it is a guide for the hand, and
      // a 4K canvas per pause is memory spent on nothing the reader can see.
      const scale = Math.min(1, 640 / state.width);
      canvas.width = Math.round(state.width * scale);
      canvas.height = Math.round(state.height * scale);
      let context = null;
      try {
        context = canvas.getContext("2d");
      } catch {
        context = null;
      }
      if (!context || video.readyState < 2) return;
      try {
        context.drawImage(video, 0, 0, canvas.width, canvas.height);
      } catch {
        // A frame that cannot be drawn leaves the plain stage; the box works
        // the same on it.
      }
    }

    function readFrame() {
      const size = frameSize();
      if (!size) return;
      const resized = size.width !== state.width || size.height !== state.height;
      state.width = size.width;
      state.height = size.height;
      if (resized) {
        if (state.touched && state.box) {
          state.box = legal(state.box);
          onChange({ ...state.box });
        } else {
          state.box = wholeFrame();
        }
      }
      drawFrame();
      render();
      askForFrame();
    }

    /** The player loads metadata only, so a video nobody has played has no
     *  frame to draw. A seek makes the browser decode one, and `seeked`
     *  draws it; asked once, since a seek per metadata event would fight the
     *  reader's own scrubbing. */
    let askedForFrame = false;
    function askForFrame() {
      if (askedForFrame || video.readyState >= 2) return;
      askedForFrame = true;
      try {
        video.currentTime = Math.max(video.currentTime || 0, 0.01);
      } catch {
        // Not seekable yet; the first pause or seek draws the still instead.
      }
    }

    function pointAt(event) {
      const rect = stage.getBoundingClientRect();
      return {
        x: clamp(((event.clientX - rect.left) / rect.width) * state.width, 0, state.width),
        y: clamp(((event.clientY - rect.top) / rect.height) * state.height, 0, state.height),
      };
    }

    function onPointerDown(event) {
      if (!state.box || event.button !== 0 || !stage.getBoundingClientRect().width) return;
      event.preventDefault();
      const point = pointAt(event);
      const corner = event.target.dataset?.corner;
      const box = { ...state.box };
      // The box starts as the whole frame, so every press lands inside it; a
      // box that already fills the frame has nowhere to move, so a press on
      // it draws a new one instead.
      const fillsFrame = box.w >= state.width - 1 && box.h >= state.height - 1;
      const inside =
        !fillsFrame && (event.target === selection || selection.contains(event.target));

      if (corner) state.drag = { mode: "resize", corner, box };
      else if (inside) state.drag = { mode: "move", start: point, box };
      else state.drag = { mode: "draw", start: point };

      if (event.pointerId !== undefined && typeof stage.setPointerCapture === "function") {
        try {
          stage.setPointerCapture(event.pointerId);
        } catch {
          // Capture is a nicety; the document listeners below still see moves.
        }
      }
      // The box is drawn on a still; a playing video would move on under it.
      if (!video.paused) video.pause();
      selection.focus?.({ preventScroll: true });
      document.addEventListener("pointermove", onPointerMove);
      document.addEventListener("pointerup", stopDragging);
      document.addEventListener("pointercancel", stopDragging);
    }

    function onPointerMove(event) {
      const drag = state.drag;
      const point = pointAt(event);
      if (drag.mode === "move") {
        setBox({
          ...drag.box,
          x: drag.box.x + point.x - drag.start.x,
          y: drag.box.y + point.y - drag.start.y,
        });
        return;
      }
      if (drag.mode === "draw") {
        const x = Math.min(drag.start.x, point.x);
        const y = Math.min(drag.start.y, point.y);
        const w = Math.abs(point.x - drag.start.x);
        const h = Math.abs(point.y - drag.start.y);
        // A click with no drag must not shrink the box to its minimum.
        if (w < 4 && h < 4) return;
        setBox(
          { x, y, w, h },
          { right: point.x < drag.start.x ? drag.start.x : undefined,
            bottom: point.y < drag.start.y ? drag.start.y : undefined },
        );
        return;
      }
      // Resize: the dragged corner follows the pointer, the opposite one stays.
      const { box, corner } = drag;
      const right = box.x + box.w;
      const bottom = box.y + box.h;
      const west = corner.includes("w");
      const north = corner.includes("n");
      const x = west ? point.x : box.x;
      const y = north ? point.y : box.y;
      setBox(
        {
          x,
          y,
          w: west ? right - point.x : point.x - box.x,
          h: north ? bottom - point.y : point.y - box.y,
        },
        { right: west ? right : undefined, bottom: north ? bottom : undefined },
      );
    }

    function stopDragging() {
      state.drag = null;
      document.removeEventListener("pointermove", onPointerMove);
      document.removeEventListener("pointerup", stopDragging);
      document.removeEventListener("pointercancel", stopDragging);
    }

    /** Arrows move the box, Shift+arrows grow or shrink it from its
     *  bottom-right corner, Escape puts the whole frame back. */
    function onKeyDown(event) {
      if (!state.box) return;
      if (event.key === "Escape") {
        event.preventDefault();
        resetBox();
        return;
      }
      const deltas = {
        ArrowLeft: [-1, 0],
        ArrowRight: [1, 0],
        ArrowUp: [0, -1],
        ArrowDown: [0, 1],
      };
      const delta = deltas[event.key];
      if (!delta) return;
      event.preventDefault();
      const [dx, dy] = delta.map((d) => d * KEY_STEP * (event.altKey ? 10 : 1));
      const box = state.box;
      if (event.shiftKey) setBox({ ...box, w: box.w + dx, h: box.h + dy });
      else setBox({ ...box, x: box.x + dx, y: box.y + dy });
    }

    function resetBox() {
      if (!state.box) return;
      setBox(wholeFrame());
    }

    function onAspectChange() {
      state.ratio = ASPECTS.find((entry) => entry.label === aspect.value)?.value ?? null;
      if (!state.box) return;
      // Re-centre the new shape on the old box, as large as it still fits.
      const { x, y, w, h } = state.box;
      const next = legal({ x, y, w, h });
      setBox({ ...next, x: x + (w - next.w) / 2, y: y + (h - next.h) / 2 });
    }

    stage.addEventListener("pointerdown", onPointerDown);
    selection.addEventListener("keydown", onKeyDown);
    reset.addEventListener("click", resetBox);
    aspect.addEventListener("change", onAspectChange);
    video.addEventListener("loadedmetadata", readFrame);
    video.addEventListener("loadeddata", readFrame);
    video.addEventListener("resize", readFrame);
    video.addEventListener("seeked", drawFrame);
    video.addEventListener("pause", drawFrame);

    readFrame();
    render();

    return function unmountCropBox() {
      stopDragging();
      video.removeEventListener("loadedmetadata", readFrame);
      video.removeEventListener("loadeddata", readFrame);
      video.removeEventListener("resize", readFrame);
      video.removeEventListener("seeked", drawFrame);
      video.removeEventListener("pause", drawFrame);
      root.remove();
    };
  }

  window.mountCropBox = mountCropBox;
})();
