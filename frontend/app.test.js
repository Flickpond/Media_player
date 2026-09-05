// @vitest-environment jsdom
//
// Tests for app.js. It is a plain browser script with no exports, so it is
// exercised the way the browser drives it: build the DOM it expects, import
// it, then interact through the form and the clock. Nothing in app.js is
// modified to make it testable.

import { readFileSync } from "node:fs";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

const here = dirname(fileURLToPath(import.meta.url));
const markup = readFileSync(join(here, "index.html"), "utf8");
const BODY = markup.slice(markup.indexOf("<body>") + 6, markup.indexOf("</body>"));

let fetchMock;

/** Rebuild the page and re-run app.js against it, as a page load would. */
async function loadApp() {
  document.body.innerHTML = BODY;
  vi.resetModules();
  await import("./app.js");
  return {
    form: document.getElementById("upload-form"),
    fileInput: document.getElementById("file-input"),
    button: document.getElementById("upload-button"),
    status: document.getElementById("status"),
    player: document.getElementById("player"),
  };
}

function chooseFile(input) {
  const file = new File(["data"], "holiday.mp4", { type: "video/mp4" });
  Object.defineProperty(input, "files", { value: [file], configurable: true });
}

const jsonResponse = (body, ok = true, status = 200) => ({
  ok,
  status,
  json: async () => body,
});

/** Submit the form and let the upload plus first poll settle. */
async function submit(el) {
  el.form.dispatchEvent(new Event("submit", { cancelable: true }));
  await vi.waitFor(() => expect(fetchMock).toHaveBeenCalled());
  await vi.advanceTimersByTimeAsync(0);
}

beforeEach(() => {
  fetchMock = vi.fn();
  globalThis.fetch = fetchMock;
  vi.useFakeTimers();
});

afterEach(() => {
  vi.useRealTimers();
  vi.restoreAllMocks();
});

describe("upload", () => {
  it("refuses to submit with no file chosen, and calls nothing", async () => {
    const el = await loadApp();

    el.form.dispatchEvent(new Event("submit", { cancelable: true }));
    await vi.waitFor(() => expect(el.status.textContent).toMatch(/choose a video file/i));

    expect(fetchMock).not.toHaveBeenCalled();
    expect(el.status.hidden).toBe(false);
  });

  it("posts the chosen file to /upload as multipart", async () => {
    const el = await loadApp();
    chooseFile(el.fileInput);
    fetchMock.mockResolvedValue(jsonResponse({ job_id: "job-1", status: "queued" }));

    await submit(el);

    const [url, init] = fetchMock.mock.calls[0];
    expect(url).toBe("http://127.0.0.1:8000/upload");
    expect(init.method).toBe("POST");
    expect(init.body.get("file")).toBeInstanceOf(File);
  });

  it("polls the job id the API returned", async () => {
    const el = await loadApp();
    chooseFile(el.fileInput);
    fetchMock
      .mockResolvedValueOnce(jsonResponse({ job_id: "abc-123" }))
      .mockResolvedValueOnce(jsonResponse({ status: "queued" }));

    await submit(el);

    expect(fetchMock.mock.calls[1][0]).toBe("http://127.0.0.1:8000/jobs/abc-123");
  });

  it("disables the button while uploading so a double click cannot double post", async () => {
    const el = await loadApp();
    chooseFile(el.fileInput);
    let release;
    fetchMock.mockReturnValueOnce(
      new Promise((resolve) => {
        release = resolve;
      }),
    );

    el.form.dispatchEvent(new Event("submit", { cancelable: true }));
    await vi.waitFor(() => expect(el.button.disabled).toBe(true));

    release(jsonResponse({ job_id: "job-1" }));
  });

  it("surfaces the API error message and re-enables the button", async () => {
    const el = await loadApp();
    chooseFile(el.fileInput);
    fetchMock.mockResolvedValueOnce(jsonResponse({ error: "file too large" }, false, 400));

    await submit(el);

    expect(el.status.textContent).toContain("file too large");
    expect(el.button.disabled).toBe(false);
    expect(fetchMock).toHaveBeenCalledTimes(1);
  });

  it("falls back to the status code when the error body is unreadable", async () => {
    const el = await loadApp();
    chooseFile(el.fileInput);
    fetchMock.mockResolvedValueOnce({
      ok: false,
      status: 500,
      json: async () => {
        throw new Error("not json");
      },
    });

    await submit(el);

    expect(el.status.textContent).toContain("500");
  });

  it("reports a network failure rather than hanging", async () => {
    const el = await loadApp();
    chooseFile(el.fileInput);
    fetchMock.mockRejectedValueOnce(new Error("connection refused"));

    await submit(el);

    expect(el.status.textContent).toContain("connection refused");
    expect(el.button.disabled).toBe(false);
  });

  it("clears a previous result when a second upload starts", async () => {
    const el = await loadApp();
    chooseFile(el.fileInput);
    fetchMock
      .mockResolvedValueOnce(jsonResponse({ job_id: "job-1" }))
      .mockResolvedValueOnce(jsonResponse({ status: "done", output_url: "http://minio/a.mp4" }));
    await submit(el);
    expect(el.player.hidden).toBe(false);

    fetchMock.mockResolvedValue(jsonResponse({ job_id: "job-2", status: "queued" }));
    await submit(el);

    expect(el.player.hidden).toBe(true);
    expect(el.player.hasAttribute("src")).toBe(false);
  });
});

describe("polling", () => {
  /** Upload, then have every poll return the given job document. */
  async function pollReturning(job) {
    const el = await loadApp();
    chooseFile(el.fileInput);
    fetchMock
      .mockResolvedValueOnce(jsonResponse({ job_id: "job-1" }))
      .mockResolvedValue(jsonResponse(job));
    await submit(el);
    return el;
  }

  it("shows the player and stops polling once the job is done", async () => {
    const el = await pollReturning({ status: "done", output_url: "http://minio/out.mp4" });

    expect(el.player.getAttribute("src")).toBe("http://minio/out.mp4");
    expect(el.player.hidden).toBe(false);
    expect(el.button.disabled).toBe(false);
    await vi.advanceTimersByTimeAsync(10000);
    expect(fetchMock).toHaveBeenCalledTimes(2);
  });

  it("shows the error and stops polling once the job has failed", async () => {
    const el = await pollReturning({ status: "failed", error: "source missing" });

    expect(el.status.textContent).toContain("source missing");
    expect(el.player.hidden).toBe(true);
    await vi.advanceTimersByTimeAsync(10000);
    expect(fetchMock).toHaveBeenCalledTimes(2);
  });

  it("does not print undefined when a failed job carries no error text", async () => {
    const el = await pollReturning({ status: "failed" });

    expect(el.status.textContent).toContain("Unknown error");
    expect(el.status.textContent).not.toContain("undefined");
  });

  it.each([
    ["queued", "Queued..."],
    ["processing", "Processing..."],
  ])("keeps polling every 2s while the job is %s", async (status, label) => {
    const el = await pollReturning({ status });
    expect(el.status.textContent).toBe(label);
    const before = fetchMock.mock.calls.length;

    await vi.advanceTimersByTimeAsync(2000);

    expect(fetchMock.mock.calls.length).toBe(before + 1);
  });

  it("stops on a network error instead of spinning in a tight loop", async () => {
    const el = await loadApp();
    chooseFile(el.fileInput);
    fetchMock
      .mockResolvedValueOnce(jsonResponse({ job_id: "job-1" }))
      .mockRejectedValue(new Error("offline"));

    await submit(el);

    expect(el.status.textContent).toContain("offline");
    expect(el.button.disabled).toBe(false);
    await vi.advanceTimersByTimeAsync(10000);
    expect(fetchMock).toHaveBeenCalledTimes(2);
  });

  it("shows an unrecognised status verbatim rather than blanking the UI", async () => {
    const el = await pollReturning({ status: "cancelled" });

    expect(el.status.textContent).toBe("cancelled");
  });

  it("a new upload cancels the previous job's scheduled poll", async () => {
    const el = await pollReturning({ status: "queued" });
    const afterFirst = fetchMock.mock.calls.length;

    fetchMock.mockResolvedValue(jsonResponse({ job_id: "job-2", status: "done" }));
    await submit(el);
    const afterSecond = fetchMock.mock.calls.length;
    await vi.advanceTimersByTimeAsync(10000);

    expect(fetchMock.mock.calls.length).toBe(afterSecond);
    expect(afterSecond).toBeGreaterThan(afterFirst);
  });
});
