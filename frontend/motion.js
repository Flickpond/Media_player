/* Rotate, mirror and speed panels for the editor's operation seam. */
(function () {
  "use strict";
  const previews = new WeakMap();

  function geometry(video, frame) {
    if (previews.has(video)) return previews.get(video);
    const original = video.style.transform;
    const state = { degrees: 0, axis: null, users: 0 };
    state.draw = () => {
      const w = video.videoWidth || frame.width;
      const h = video.videoHeight || frame.height;
      const boxW = video.clientWidth;
      const boxH = video.clientHeight;
      let fit = 1;
      if (w && h && boxW && boxH && state.degrees % 180 !== 0) {
        const ratio = Math.min(boxW / w, boxH / h);
        fit = Math.min(boxW / (h * ratio), boxH / (w * ratio));
      }
      // CSS applies the rightmost transform first: turn, then mirror, as FFmpeg does.
      video.style.transform = state.degrees || state.axis
        ? `scale(${fit}) scaleX(${state.axis === "horizontal" ? -1 : 1}) ` +
          `scaleY(${state.axis === "vertical" ? -1 : 1}) rotate(${state.degrees}deg)`
        : original;
    };
    state.resize = typeof ResizeObserver === "function" ? new ResizeObserver(state.draw) : null;
    state.resize?.observe(video);
    video.addEventListener("loadedmetadata", state.draw);
    window.addEventListener("resize", state.draw);
    state.release = () => {
      if (--state.users > 0) return;
      state.resize?.disconnect();
      video.removeEventListener("loadedmetadata", state.draw);
      window.removeEventListener("resize", state.draw);
      video.style.transform = original;
      previews.delete(video);
    };
    previews.set(video, state);
    return state;
  }

  function mountChoices(video, onChange, frame, name, values, labels, key) {
    const section = document.querySelector(`[data-operation="${name}"]`);
    const host = section.querySelector(".edit-mount");
    const checkbox = section.querySelector('input[type="checkbox"]');
    const state = geometry(video, frame);
    state.users++;
    let value = values[0];
    const group = document.createElement("div");
    group.className = "motion-choices";
    group.setAttribute("role", "group");
    group.setAttribute("aria-label", name === "rotate" ? "Clockwise rotation" : "Mirror axis");
    const buttons = values.map((choice, index) => {
      const button = document.createElement("button");
      button.type = "button";
      button.textContent = labels[index];
      button.addEventListener("click", () => { value = choice; update(); });
      group.append(button);
      return button;
    });
    const note = document.createElement("p");
    note.className = "hint";
    note.textContent = name === "rotate"
      ? "Clockwise, after crop and before scale. Tick Rotate to preview."
      : "Mirror after rotation. Choose one axis; tick Mirror to preview.";
    host.replaceChildren(group, note);
    function update() {
      buttons.forEach((button, i) => button.setAttribute("aria-pressed", String(value === values[i])));
      state[name === "rotate" ? "degrees" : "axis"] = checkbox.checked ? value : name === "rotate" ? 0 : null;
      state.draw();
      onChange({ [key]: value });
    }
    checkbox.addEventListener("change", update);
    update();
    return () => {
      checkbox.removeEventListener("change", update);
      state[name === "rotate" ? "degrees" : "axis"] = name === "rotate" ? 0 : null;
      state.draw();
      state.release();
      host.replaceChildren();
    };
  }

  window.mountRotatePanel = (video, onChange, frame = {}) => mountChoices(
    video, onChange, frame, "rotate", [90, 180, 270], ["90°", "180°", "270°"], "degrees",
  );
  window.mountFlipPanel = (video, onChange, frame = {}) => mountChoices(
    video, onChange, frame, "flip", ["horizontal", "vertical"], ["Horizontal", "Vertical"], "axis",
  );

  window.mountSpeedPanel = (video, onChange) => {
    const section = document.querySelector('[data-operation="speed"]');
    const host = section.querySelector(".edit-mount");
    const checkbox = section.querySelector('input[type="checkbox"]');
    const originalRate = video.playbackRate;
    const originalPitch = video.preservesPitch;
    const label = document.createElement("label");
    label.className = "field";
    label.textContent = "Playback speed ";
    const select = document.createElement("select");
    select.className = "motion-speed";
    for (let factor = 0.5; factor <= 2; factor += 0.25) {
      const option = document.createElement("option");
      option.value = String(factor);
      option.textContent = `${factor}×`;
      select.append(option);
    }
    select.value = "1";
    label.append(select);
    const note = document.createElement("p");
    note.className = "hint";
    note.textContent = "Keeps audio pitch. Clip times refer to the original timeline.";
    host.replaceChildren(label, note);
    function update() {
      const factor = Number(select.value);
      video.preservesPitch = checkbox.checked ? true : originalPitch;
      video.playbackRate = checkbox.checked ? factor : originalRate;
      onChange({ factor });
    }
    select.addEventListener("change", update);
    checkbox.addEventListener("change", update);
    update();
    return () => {
      select.removeEventListener("change", update);
      checkbox.removeEventListener("change", update);
      video.playbackRate = originalRate;
      video.preservesPitch = originalPitch;
      host.replaceChildren();
    };
  };
})();
