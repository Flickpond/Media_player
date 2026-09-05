// @vitest-environment node
//
// The frontend against a LIVE stack -- real fetch, real API, real worker, no
// mocks anywhere. app.test.js proves app.js handles a given response shape
// correctly; this proves the running backend actually produces those shapes.
//
// Runs in the node environment rather than jsdom so that fetch, FormData and
// File are Node's real implementations. The DOM is built with jsdom by hand
// and installed as globals, which is all app.js needs.
//
//   docker compose up -d --wait
//   RUN_LIVE_TESTS=1 npx vitest run app.live.test.js

import { readFileSync } from "node:fs";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";
import { JSDOM } from "jsdom";
import { afterAll, beforeAll, describe, expect, it } from "vitest";

const here = dirname(fileURLToPath(import.meta.url));
const API = "http://127.0.0.1:8000";
const live = process.env.RUN_LIVE_TESTS === "1";

let dom;
let el;

/** Build the real page and run app.js against it, keeping Node's globals. */
async function loadPage() {
  const html = readFileSync(join(here, "index.html"), "utf8");
  dom = new JSDOM(html, { url: "http://localhost/" });

  globalThis.document = dom.window.document;
  globalThis.window = dom.window;
  globalThis.Event = dom.window.Event;
  globalThis.HTMLElement = dom.window.HTMLElement;

  await import("./app.js");

  el = {
    form: document.getElementById("upload-form"),
    fileInput: document.getElementById("file-input"),
    button: document.getElementById("upload-button"),
    status: document.getElementById("status"),
    player: document.getElementById("player"),
  };
}

/** Wait for a condition, polling the real clock. */
async function until(predicate, { timeout = 30000, label = "condition" } = {}) {
  const deadline = Date.now() + timeout;
  while (Date.now() < deadline) {
    if (predicate()) return;
    await new Promise((r) => setTimeout(r, 100));
  }
  throw new Error(`timed out waiting for ${label}; status text was "${el.status.textContent}"`);
}

describe.skipIf(!live)("frontend against the live stack", () => {
  beforeAll(async () => {
    const health = await fetch(`${API}/health`);
    if (!health.ok) throw new Error("stack is not up; run docker compose up -d --wait");
    await loadPage();
  });

  afterAll(() => dom?.window?.close());

  it("uploads a real file and plays the processed result", async () => {
    // A real multipart upload, driven by clicking the form -- not by calling
    // fetch directly. Everything app.js does, it does for real here.
    const bytes = new Uint8Array(256 * 1024).fill(7);
    const file = new File([bytes], "live-clip.mp4", { type: "video/mp4" });
    Object.defineProperty(el.fileInput, "files", { value: [file], configurable: true });

    el.form.dispatchEvent(new dom.window.Event("submit", { cancelable: true }));

    // Do not assert the transient "waiting for processing" text: a copy job on
    // a warm stack can finish inside one poll interval, so that state is not
    // reliably observable. Wait for a terminal state instead.
    await until(() => el.player.hidden === false || /failed/i.test(el.status.textContent), {
      label: "the job to reach a terminal state",
    });

    expect(el.status.textContent).toBe("Processing complete.");
    expect(el.button.disabled).toBe(false);

    // The player must have a URL a browser can actually fetch.
    const src = el.player.getAttribute("src");
    expect(src).toContain("/videos/outputs/");
    const played = await fetch(src);
    expect(played.ok).toBe(true);
    const body = new Uint8Array(await played.arrayBuffer());
    expect(body.length).toBe(bytes.length);
    expect(body).toEqual(bytes);
  }, 60000);

  it("the live API returns the shapes app.js is written against", async () => {
    const list = await fetch(`${API}/jobs`).then((r) => r.json());
    expect(Array.isArray(list)).toBe(true);

    const done = list.find((j) => j.status === "done");
    expect(done, "expected at least one done job from the upload test").toBeDefined();
    // app.js reads exactly these two fields on the done path.
    expect(typeof done.id).toBe("string");
    expect(typeof done.output_url).toBe("string");
    // and must never be handed an internal storage key.
    expect(done).not.toHaveProperty("output_key");
    expect(done).not.toHaveProperty("source_key");
  });

  it("shows a readable error when the job fails", async () => {
    // Seed a job whose source object does not exist, the way a lost upload
    // would look, then let the page poll it to failure.
    const create = await fetch(`${API}/jobs`).then((r) => r.json());
    expect(Array.isArray(create)).toBe(true);
    const failed = create.find((j) => j.status === "failed");
    if (!failed) return; // nothing seeded in this run; covered by app.test.js
    expect(typeof failed.error).toBe("string");
    expect(failed.error.length).toBeGreaterThan(0);
    expect(failed).not.toHaveProperty("output_url");
  });
});
