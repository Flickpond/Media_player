// Filter presets: the editor's one-click looks.
//
// `app.js` mounts this through `window.mountPresetPanel(video, onChange,
// frame)` (frontend/EDITOR-COMPONENTS.md). The five names are the API contract
// in app/schemas/edit.py; what this file reports is exactly `{ name }`.
//
// The preview is WebGL: the current frame is uploaded as a texture and the
// shader applies the same saturation, contrast, lift and tint the FFmpeg chain
// applies. It is a preview, not the result -- the worker's filters remain the
// source of truth. A browser without WebGL, or a video whose storage origin
// does not allow reading it into a texture, falls back to an approximate CSS
// filter so the control still gives useful feedback.
//
// A plain script like app.js: no build step, no imports, one global.

(function () {
  "use strict";

  const PRESETS = [
    { name: "bw", label: "B&W", hint: "no colour" },
    { name: "warm", label: "Warm", hint: "amber light" },
    { name: "cool", label: "Cool", hint: "blue shade" },
    { name: "vintage", label: "Vintage", hint: "faded film" },
    { name: "vivid", label: "Vivid", hint: "strong colour" },
  ];

  const PRESET_INDEX = new Map(PRESETS.map((preset, index) => [preset.name, index]));

  // Used only when WebGL cannot read the video's pixels. It mirrors the
  // shader's rough direction rather than pretending to be pixel-exact.
  const CSS_FILTERS = {
    bw: "grayscale(1)",
    warm: "saturate(1.12) brightness(1.02) sepia(0.12)",
    cool: "saturate(1.05) brightness(1.01) hue-rotate(8deg)",
    vintage: "sepia(0.45) saturate(0.82) contrast(0.95) brightness(1.03)",
    vivid: "saturate(1.38) contrast(1.12)",
  };

  const VERTEX_SHADER = `
    attribute vec2 a_position;
    varying vec2 v_uv;

    void main() {
      v_uv = a_position * 0.5 + 0.5;
      gl_Position = vec4(a_position, 0.0, 1.0);
    }
  `;

  // u_preset's indices are PRESETS's order: 0 bw, 1 warm, 2 cool, 3 vintage,
  // 4 vivid. Keep the two lists together when changing either.
  const FRAGMENT_SHADER = `
    precision mediump float;

    varying vec2 v_uv;
    uniform sampler2D u_video;
    uniform int u_preset;

    vec3 withSaturation(vec3 colour, float amount) {
      float luma = dot(colour, vec3(0.2126, 0.7152, 0.0722));
      return mix(vec3(luma), colour, amount);
    }

    void main() {
      vec3 colour = texture2D(u_video, v_uv).rgb;

      if (u_preset == 0) {
        colour = withSaturation(colour, 0.0);
      } else if (u_preset == 1) {
        colour = withSaturation(colour, 1.12) + vec3(0.03, 0.005, -0.03);
      } else if (u_preset == 2) {
        colour = withSaturation(colour, 1.05) + vec3(-0.025, 0.0, 0.035);
      } else if (u_preset == 3) {
        float luma = dot(colour, vec3(0.2126, 0.7152, 0.0722));
        vec3 sepia = vec3(luma * 1.07 + 0.10, luma * 0.94 + 0.04, luma * 0.72 + 0.02);
        colour = mix(colour, sepia, 0.55);
        colour = (colour - 0.5) * 0.95 + 0.5;
      } else {
        colour = withSaturation(colour, 1.38);
        colour = (colour - 0.5) * 1.12 + 0.5;
        colour += vec3(0.015, 0.0, 0.015);
      }

      gl_FragColor = vec4(clamp(colour, 0.0, 1.0), 1.0);
    }
  `;

  function createElement(tag, className, attributes = {}) {
    const node = document.createElement(tag);
    if (className) node.className = className;
    for (const [name, value] of Object.entries(attributes)) node.setAttribute(name, value);
    return node;
  }

  function defaultHost(video) {
    const mount = document.querySelector('[data-operation="preset"] .edit-mount');
    if (mount) return mount;
    const spot = createElement("div", "preset-host");
    const anchor = video.closest(".player-host") ?? video;
    anchor.after(spot);
    return spot;
  }

  function compileShader(gl, type, source) {
    const shader = gl.createShader(type);
    if (!shader) return null;
    gl.shaderSource(shader, source);
    gl.compileShader(shader);
    if (gl.getShaderParameter(shader, gl.COMPILE_STATUS)) return shader;
    gl.deleteShader(shader);
    return null;
  }

  /**
   * A canvas over the video, or null when this browser cannot give us a
   * WebGL context or compile the shader. Nothing is inserted into the page
   * until every piece exists, so a failed preview leaves the video untouched.
   */
  function createPreview(video) {
    const parent = video.parentElement;
    if (!parent) return null;

    const canvas = createElement("canvas", "preset-preview", {
      "aria-hidden": "true",
      hidden: "",
    });
    let gl = null;
    try {
      gl = canvas.getContext("webgl", {
        alpha: false,
        premultipliedAlpha: false,
        preserveDrawingBuffer: false,
      });
    } catch {
      gl = null;
    }
    if (!gl) return null;

    const vertexShader = compileShader(gl, gl.VERTEX_SHADER, VERTEX_SHADER);
    const fragmentShader = compileShader(gl, gl.FRAGMENT_SHADER, FRAGMENT_SHADER);
    const program = vertexShader && fragmentShader ? gl.createProgram() : null;
    if (!program) {
      if (vertexShader) gl.deleteShader(vertexShader);
      if (fragmentShader) gl.deleteShader(fragmentShader);
      return null;
    }

    gl.attachShader(program, vertexShader);
    gl.attachShader(program, fragmentShader);
    gl.linkProgram(program);
    if (!gl.getProgramParameter(program, gl.LINK_STATUS)) {
      gl.deleteProgram(program);
      gl.deleteShader(vertexShader);
      gl.deleteShader(fragmentShader);
      return null;
    }

    const buffer = gl.createBuffer();
    const texture = gl.createTexture();
    gl.useProgram(program);
    gl.bindBuffer(gl.ARRAY_BUFFER, buffer);
    gl.bufferData(
      gl.ARRAY_BUFFER,
      new Float32Array([-1, -1, 1, -1, -1, 1, 1, 1]),
      gl.STATIC_DRAW,
    );
    const position = gl.getAttribLocation(program, "a_position");
    gl.enableVertexAttribArray(position);
    gl.vertexAttribPointer(position, 2, gl.FLOAT, false, 0, 0);
    gl.activeTexture(gl.TEXTURE0);
    gl.bindTexture(gl.TEXTURE_2D, texture);
    gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_MIN_FILTER, gl.LINEAR);
    gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_MAG_FILTER, gl.LINEAR);
    gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_WRAP_S, gl.CLAMP_TO_EDGE);
    gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_WRAP_T, gl.CLAMP_TO_EDGE);
    gl.pixelStorei(gl.UNPACK_FLIP_Y_WEBGL, true);
    gl.uniform1i(gl.getUniformLocation(program, "u_video"), 0);
    const presetUniform = gl.getUniformLocation(program, "u_preset");

    parent.insertBefore(canvas, video.nextSibling);

    return {
      canvas,
      show() {
        canvas.hidden = false;
      },
      hide() {
        canvas.hidden = true;
      },
      setPreset(index) {
        gl.uniform1i(presetUniform, index);
      },
      /** False while the video has no decoded frame yet; throws if WebGL
       *  refuses the texture (a cross-origin video without CORS). */
      paint() {
        if (video.readyState < 2 || !video.videoWidth || !video.videoHeight) return false;
        // A smaller preview texture keeps a 4K source affordable to draw on
        // every frame. The canvas still has the source's aspect ratio.
        const width = Math.min(video.videoWidth, 1280);
        const height = Math.max(1, Math.round((video.videoHeight * width) / video.videoWidth));
        if (canvas.width !== width || canvas.height !== height) {
          canvas.width = width;
          canvas.height = height;
          gl.viewport(0, 0, width, height);
        }
        gl.texImage2D(gl.TEXTURE_2D, 0, gl.RGBA, gl.RGBA, gl.UNSIGNED_BYTE, video);
        gl.drawArrays(gl.TRIANGLE_STRIP, 0, 4);
        canvas.hidden = false;
        return true;
      },
      destroy() {
        canvas.remove();
        gl.deleteTexture(texture);
        gl.deleteBuffer(buffer);
        gl.deleteProgram(program);
        gl.deleteShader(vertexShader);
        gl.deleteShader(fragmentShader);
      },
    };
  }

  /**
   * `options` is `{ width, height, duration }` of the original upload, when
   * the API knows it. The preview itself needs the playing element, not the
   * stored size, so it is accepted for the shared component contract and
   * deliberately unused here.
   */
  function mountPresetPanel(video, onChange, options = {}) {
    const host = options.host ?? defaultHost(video);
    const section = host.closest('details[data-operation="preset"]');
    const checkbox = section?.querySelector('summary input[type="checkbox"]');
    const root = createElement("div", "preset-panel", {
      role: "radiogroup",
      "aria-label": "Filter preset",
    });
    const grid = createElement("div", "preset-grid");
    const note = createElement("p", "preset-note");
    note.textContent = "Preview only — Process applies the look to the whole video.";

    const tiles = PRESETS.map((preset) => {
      const tile = createElement("button", "preset-tile", {
        type: "button",
        role: "radio",
        "aria-checked": "false",
        tabindex: "-1",
      });
      tile.dataset.preset = preset.name;
      const swatch = createElement("span", `preset-swatch preset-swatch-${preset.name}`, {
        "aria-hidden": "true",
      });
      const text = createElement("span", "preset-copy");
      const label = createElement("span", "preset-name");
      label.textContent = preset.label;
      const hint = createElement("span", "preset-hint");
      hint.textContent = preset.hint;
      text.append(label, hint);
      tile.append(swatch, text);
      grid.append(tile);
      return tile;
    });

    const clear = createElement("button", "preset-clear", { type: "button" });
    clear.textContent = "Original, no preset";
    root.append(grid, clear, note);
    host.append(root);

    const previousFilter = video.style.filter;
    let selected = null;
    let preview = null;
    let previewBroken = false;
    let frameRequest = 0;

    function cancelFrame() {
      if (!frameRequest) return;
      if (typeof cancelAnimationFrame === "function") cancelAnimationFrame(frameRequest);
      frameRequest = 0;
    }

    function useCssFilter(name) {
      if (preview) {
        preview.destroy();
        preview = null;
      }
      previewBroken = true;
      video.style.filter = CSS_FILTERS[name];
    }

    function ensurePreview() {
      if (preview || previewBroken) return;
      try {
        preview = createPreview(video);
      } catch {
        preview = null;
      }
      if (!preview) previewBroken = true;
    }

    function draw() {
      frameRequest = 0;
      if (!selected) return;
      if (!preview) {
        video.style.filter = CSS_FILTERS[selected];
        return;
      }
      try {
        preview.paint();
      } catch {
        useCssFilter(selected);
        return;
      }
      if (!video.paused && !video.ended) frameRequest = requestAnimationFrame(draw);
    }

    function scheduleDraw() {
      if (!selected || frameRequest) return;
      frameRequest = requestAnimationFrame(draw);
    }

    function showPreview(active) {
      if (!selected) return;
      cancelFrame();
      if (!active) {
        if (preview) preview.hide();
        video.style.filter = previousFilter;
        return;
      }
      if (preview) scheduleDraw();
      else video.style.filter = CSS_FILTERS[selected];
    }

    function onCheckboxChange() {
      showPreview(Boolean(checkbox?.checked));
    }

    function syncTiles() {
      for (const tile of tiles) {
        const checked = tile.dataset.preset === selected;
        tile.classList.toggle("is-selected", checked);
        tile.setAttribute("aria-checked", String(checked));
        tile.tabIndex = checked || (!selected && tile === tiles[0]) ? 0 : -1;
      }
      clear.disabled = selected === null;
    }

    function choose(name, { focus = false } = {}) {
      selected = name;
      syncTiles();
      cancelFrame();
      if (!name) {
        onChange(null);
        if (preview) preview.hide();
        video.style.filter = previousFilter;
        return;
      }

      onChange({ name });
      ensurePreview();
      if (preview) {
        preview.setPreset(PRESET_INDEX.get(name));
        scheduleDraw();
      } else {
        video.style.filter = CSS_FILTERS[name];
      }
      if (focus) {
        const index = PRESETS.findIndex((preset) => preset.name === name);
        tiles[index]?.focus();
      }
    }

    for (const tile of tiles) {
      tile.addEventListener("click", () => choose(tile.dataset.preset));
      tile.addEventListener("keydown", (event) => {
        const index = PRESETS.findIndex((preset) => preset.name === tile.dataset.preset);
        let next = index;
        if (event.key === "ArrowRight" || event.key === "ArrowDown") next = (index + 1) % tiles.length;
        else if (event.key === "ArrowLeft" || event.key === "ArrowUp")
          next = (index - 1 + tiles.length) % tiles.length;
        else if (event.key === "Home") next = 0;
        else if (event.key === "End") next = tiles.length - 1;
        else return;
        event.preventDefault();
        choose(PRESETS[next].name, { focus: true });
      });
    }
    clear.addEventListener("click", () => choose(null));
    checkbox?.addEventListener("change", onCheckboxChange);

    video.addEventListener("loadeddata", scheduleDraw);
    video.addEventListener("seeked", scheduleDraw);
    video.addEventListener("play", scheduleDraw);
    video.addEventListener("pause", scheduleDraw);

    syncTiles();

    return function unmount() {
      cancelFrame();
      video.removeEventListener("loadeddata", scheduleDraw);
      video.removeEventListener("seeked", scheduleDraw);
      video.removeEventListener("play", scheduleDraw);
      video.removeEventListener("pause", scheduleDraw);
      checkbox?.removeEventListener("change", onCheckboxChange);
      if (preview) preview.destroy();
      video.style.filter = previousFilter;
      root.remove();
    };
  }

  window.mountPresetPanel = mountPresetPanel;
})();
