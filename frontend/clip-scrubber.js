// The clip scrubber: Track C's half of the editor's component seam.
//
// The editing panel in app.js looks up `window.mountClipScrubber(video,
// onChange)` when it opens (EDITOR-COMPONENTS.md) and sends whatever this
// reports as the clip operation's params. So everything here is about one
// promise: the `{ start, end }` handed to onChange is the range the reader
// sees, in seconds on the timeline of the video they are watching, with
// `0 <= start < end <= duration` -- the three things the worker's
// validate_clip refuses otherwise.
//
// A plain script like app.js: no build step, no imports, one global.

(function () {
  "use strict";

  /** The shortest clip the scrubber will make, on each grid. The worker only
   *  needs end > start; one step of the grid is the finest the readout shows. */
  const MIN_LENGTH_FINE = 0.1;
  const MIN_LENGTH_COARSE = 1;
  /** Up to this long, the scrubber works and shows tenths of a second.
   *  Longer than this a pixel of track is already more than a second of
   *  video, so it works in whole seconds and the readout is plain m:ss. */
  const FINE_UP_TO_SECONDS = 60;

  function pad2(n) {
    return String(n).padStart(2, "0");
  }

  /** `m:ss`, or `m:ss.s` when the scrubber works in tenths. The value is
   *  already on the scrubber's grid, so this is display, never rounding. */
  function formatClock(seconds, fine) {
    const tenths = Math.round(seconds * 10);
    const whole = Math.floor(tenths / 10);
    const clock = `${Math.floor(whole / 60)}:${pad2(whole % 60)}`;
    return fine ? `${clock}.${tenths % 10}` : clock;
  }

  /** The selected length, said the way a person would check it. */
  function formatLength(seconds, fine) {
    return seconds < 60 ? `${(Math.round(seconds * 10) / 10).toFixed(1)} s` : formatClock(seconds, fine);
  }

  /** Snap a time to the scrubber's grid without picking up binary fractions:
   *  2.3 has to go on the wire as 2.3, not 2.3000000000000003. */
  function snap(seconds, fine) {
    return fine ? Math.round(seconds * 10) / 10 : Math.round(seconds);
  }

  function clamp(value, low, high) {
    return Math.min(Math.max(value, low), high);
  }

  /** Where to draw, when the page did not say. The editor mounts the
   *  scrubber into this div (index.html); anywhere else, right after the
   *  player is the next best thing. */
  function defaultHost(video) {
    const host = document.getElementById("edit-clip-mount");
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

  function mountClipScrubber(video, onChange, host = defaultHost(video)) {
    const root = createElement("div", "clip-scrubber");
    const track = createElement("div", "clip-track");
    const range = createElement("div", "clip-range");
    const playhead = createElement("div", "clip-playhead", { "aria-hidden": "true" });
    const handles = {
      start: createElement("div", "clip-handle clip-handle-start", {
        role: "slider",
        "aria-label": "Clip start",
      }),
      end: createElement("div", "clip-handle clip-handle-end", {
        role: "slider",
        "aria-label": "Clip end",
      }),
    };
    const readout = createElement("p", "clip-readout", { "aria-live": "polite" });
    const readoutRange = createElement("span", "clip-readout-range");
    const readoutLength = createElement("span", "clip-readout-length");
    readout.append(readoutRange, readoutLength);
    track.append(range, playhead, handles.start, handles.end);
    root.append(track, readout);
    host.append(root);

    const state = {
      duration: null,
      maxEnd: 0,
      fine: true,
      start: 0,
      end: 0,
      /** Until the reader moves something, there is no selection to report:
       *  the panel keeps Process disabled for a ticked Clip with no range. */
      touched: false,
      drag: null,
    };

    function gap() {
      return state.fine ? MIN_LENGTH_FINE : MIN_LENGTH_COARSE;
    }

    function usable() {
      return state.duration !== null && state.maxEnd >= gap();
    }

    function percent(seconds) {
      return `${(seconds / state.duration) * 100}%`;
    }

    function render() {
      const ready = usable();
      for (const [name, handle] of Object.entries(handles)) {
        handle.tabIndex = ready ? 0 : -1;
        handle.setAttribute("aria-disabled", String(!ready));
        if (!ready) continue;
        const value = state[name];
        const [low, high] =
          name === "start"
            ? [0, state.end - gap()]
            : [state.start + gap(), state.maxEnd];
        handle.setAttribute("aria-valuemin", String(toTenth(low)));
        handle.setAttribute("aria-valuemax", String(toTenth(high)));
        handle.setAttribute("aria-valuenow", String(value));
        handle.setAttribute("aria-valuetext", formatClock(value, state.fine));
        handle.style.left = percent(value);
      }
      root.classList.toggle("is-disabled", !ready);

      if (state.duration === null) {
        readoutRange.textContent = "Loading the video's length…";
        readoutLength.textContent = "";
        return;
      }
      if (!ready) {
        readoutRange.textContent = "This video is too short to clip.";
        readoutLength.textContent = "";
        return;
      }
      range.style.left = percent(state.start);
      range.style.width = percent(state.end - state.start);
      readoutRange.textContent = `${formatClock(state.start, state.fine)} – ${formatClock(state.end, state.fine)}`;
      readoutLength.textContent = formatLength(state.end - state.start, state.fine);
    }

    /** Bounds worked out by subtraction (end - gap) pick up binary
     *  fractions; this puts them back on the tenths the readout shows. */
    function toTenth(seconds) {
      return Math.round(seconds * 10) / 10;
    }

    function renderPlayhead() {
      const time = video.currentTime;
      if (!usable() || !Number.isFinite(time)) {
        playhead.hidden = true;
        return;
      }
      playhead.hidden = false;
      playhead.style.left = percent(clamp(time, 0, state.duration));
    }

    /** Take the length from the element, the only measure the component has.
     *  The end never goes past it -- rounded down to a tenth, so a clip that
     *  runs "to the end" stays inside the worker's own probe of the file. */
    function readDuration() {
      const duration = video.duration;
      if (!Number.isFinite(duration) || duration <= 0) return;
      if (duration === state.duration) return;

      state.duration = duration;
      state.fine = duration <= FINE_UP_TO_SECONDS;
      state.maxEnd = Math.floor(duration * 10 + 1e-6) / 10;
      if (state.touched) {
        state.end = Math.min(state.end, state.maxEnd);
        state.start = Math.max(0, Math.min(state.start, toTenth(state.end - gap())));
      } else {
        state.start = 0;
        state.end = state.maxEnd;
      }
      render();
      renderPlayhead();
      if (state.touched && usable()) report();
    }

    function report() {
      onChange({ start: state.start, end: state.end });
    }

    function seek(seconds) {
      try {
        video.currentTime = seconds;
      } catch {
        // Not seekable yet; the next move tries again.
      }
    }

    /** Move one handle, keeping 0 <= start < end <= maxEnd however it is
     *  asked -- the dragging maths can overshoot, the keyboard can push
     *  against the other handle, and neither may break the range. */
    function setValue(name, seconds) {
      let value = clamp(snap(seconds, state.fine), 0, state.maxEnd);
      if (name === "start") {
        value = clamp(value, 0, toTenth(state.end - gap()));
      } else {
        // The last stop is the length itself, which on the whole-second grid
        // can sit between two grid lines.
        if (seconds >= state.maxEnd) value = state.maxEnd;
        value = clamp(value, toTenth(state.start + gap()), state.maxEnd);
      }

      const changed = value !== state[name] || !state.touched;
      state[name] = value;
      state.touched = true;
      render();
      seek(value);
      if (changed) report();
    }

    function timeAt(clientX) {
      const box = track.getBoundingClientRect();
      return ((clientX - box.left) / box.width) * state.duration;
    }

    function pixelsBetween(a, b) {
      const box = track.getBoundingClientRect();
      return (Math.abs(a - b) / state.duration) * box.width;
    }

    /** Which handle a press takes. The one under the pointer, or the nearer
     *  one for a press on the bare track -- except when the two sit on top of
     *  each other, where the first move decides: left takes the start, right
     *  the end. Otherwise a handle parked on the other could never get out. */
    function pickHandle(target, time) {
      if (pixelsBetween(state.start, state.end) < 6) return null;
      if (target === handles.start) return "start";
      if (target === handles.end) return "end";
      return Math.abs(time - state.start) <= Math.abs(time - state.end) ? "start" : "end";
    }

    function onPointerDown(event) {
      // A track with no width (its section still folded away) has no
      // seconds to give a position; nothing can be pressed there anyway.
      if (!usable() || event.button !== 0 || !track.getBoundingClientRect().width) return;
      event.preventDefault();
      const time = timeAt(event.clientX);
      const name = pickHandle(event.target, time);
      const onHandle = event.target === handles.start || event.target === handles.end;
      // Holding a handle a few pixels off its centre must not make it jump by
      // those pixels on the first move: keep the distance and drag by it.
      state.drag = { name, x: event.clientX, offset: name && onHandle ? time - state[name] : 0 };

      if (event.pointerId !== undefined && typeof track.setPointerCapture === "function") {
        try {
          track.setPointerCapture(event.pointerId);
        } catch {
          // Capture is a nicety; the document listeners below still see moves.
        }
      }
      // Seeking a playing video under a moving handle fights the playback:
      // the frame shown would be wherever playback got to, not the handle.
      if (!video.paused) video.pause();
      if (name) {
        handles[name].focus?.({ preventScroll: true });
        // A press on the bare track jumps that handle to the pointer; a
        // press on a handle only takes hold of it.
        if (!onHandle) setValue(name, time);
      }
      document.addEventListener("pointermove", onPointerMove);
      document.addEventListener("pointerup", onPointerUp);
      document.addEventListener("pointercancel", onPointerUp);
    }

    function onPointerMove(event) {
      // Only listened to between a press and its release, so a drag is on.
      const drag = state.drag;
      if (!drag.name) {
        if (event.clientX === drag.x) return;
        drag.name = event.clientX < drag.x ? "start" : "end";
        drag.offset = timeAt(drag.x) - state[drag.name];
        handles[drag.name].focus?.({ preventScroll: true });
      }
      setValue(drag.name, timeAt(event.clientX) - drag.offset);
    }

    function stopDragging() {
      state.drag = null;
      document.removeEventListener("pointermove", onPointerMove);
      document.removeEventListener("pointerup", onPointerUp);
      document.removeEventListener("pointercancel", onPointerUp);
    }

    function onPointerUp() {
      stopDragging();
    }

    /** Arrow keys step by the grid (a tenth, or a second on a long video),
     *  Shift by ten steps, Page keys by a tenth of the video, Home and End to
     *  as far as that handle can go. */
    function onKeyDown(event) {
      if (!usable()) return;
      const name = event.currentTarget === handles.start ? "start" : "end";
      const step = (state.fine ? 0.1 : 1) * (event.shiftKey ? 10 : 1);
      const page = state.duration / 10;
      const current = state[name];
      const moves = {
        ArrowLeft: current - step,
        ArrowDown: current - step,
        ArrowRight: current + step,
        ArrowUp: current + step,
        PageDown: current - page,
        PageUp: current + page,
        Home: 0,
        End: state.maxEnd,
      };
      if (!(event.key in moves)) return;
      event.preventDefault();
      setValue(name, moves[event.key]);
    }

    track.addEventListener("pointerdown", onPointerDown);
    handles.start.addEventListener("keydown", onKeyDown);
    handles.end.addEventListener("keydown", onKeyDown);
    video.addEventListener("loadedmetadata", readDuration);
    video.addEventListener("durationchange", readDuration);
    video.addEventListener("timeupdate", renderPlayhead);
    video.addEventListener("seeked", renderPlayhead);

    render();
    readDuration();
    renderPlayhead();

    return function unmountClipScrubber() {
      stopDragging();
      video.removeEventListener("loadedmetadata", readDuration);
      video.removeEventListener("durationchange", readDuration);
      video.removeEventListener("timeupdate", renderPlayhead);
      video.removeEventListener("seeked", renderPlayhead);
      root.remove();
    };
  }

  window.mountClipScrubber = mountClipScrubber;
})();
