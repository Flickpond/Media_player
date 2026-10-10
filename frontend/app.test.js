// @vitest-environment jsdom
//
// Tests for app.js. It is a plain browser script with no exports, so it is
// exercised the way the browser drives it: build the DOM it expects, import
// it, then interact through the dropzone, the nav, and the clock. Nothing in
// app.js is modified to make it testable.

import { readFileSync } from "node:fs";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

const here = dirname(fileURLToPath(import.meta.url));
const markup = readFileSync(join(here, "index.html"), "utf8");
const BODY = markup.slice(markup.indexOf("<body>") + 6, markup.indexOf("</body>"));

let fetchMock;

/** Rebuild the page and re-run app.js against it, as a page load would.
 *
 * app.js asks GET /auth/me on load, because the session cookie is HttpOnly and
 * the page cannot tell whether it is signed in by looking. That call is
 * answered here and then cleared from the mock, so each test's assertions
 * index from its own first request rather than from the identity check.
 * `loadSignedOut` is for the tests that care about the signed-out half.
 */
async function loadApp({ signedIn = true, role = "user", limits = null } = {}) {
  document.body.innerHTML = BODY;
  vi.resetModules();
  fetchMock.mockResolvedValueOnce(
    signedIn
      ? jsonResponse({ id: "u-1", email: "maya@example.test", role })
      : jsonResponse({ error: "not authenticated" }, false, 401),
  );
  // app.js reads the public /limits right after it asks who it is talking to.
  // A server from before sprint 4 has no such route, which is the default
  // here; `limits` lets a test say what this deployment allows.
  fetchMock.mockResolvedValueOnce(limits ? jsonResponse(limits) : jsonResponse({ error: "not found" }, false, 404));
  await import("./app.js");
  await vi.waitFor(() => expect(fetchMock).toHaveBeenCalledWith("/api/auth/me"));
  await vi.advanceTimersByTimeAsync(0);
  fetchMock.mockClear();
  return {
    dropzone: document.getElementById("dropzone"),
    dropzoneHint: document.getElementById("dropzone-hint"),
    resumePrompt: document.getElementById("resume-prompt"),
    resumeText: document.getElementById("resume-text"),
    resumeChoose: document.getElementById("resume-choose"),
    resumeDiscard: document.getElementById("resume-discard"),
    fileInput: document.getElementById("file-input"),
    browseButton: document.getElementById("browse-button"),
    status: document.getElementById("status"),
    activeJob: document.getElementById("active-job"),
    jobFilename: document.getElementById("job-filename"),
    jobBadge: document.getElementById("job-badge"),
    jobDelete: document.getElementById("job-delete"),
    jobProgress: document.getElementById("job-progress"),
    uploadProgress: document.getElementById("upload-progress"),
    uploadProgressTrack: document.getElementById("upload-progress-track"),
    uploadProgressFill: document.getElementById("upload-progress-fill"),
    uploadProgressPercent: document.getElementById("upload-progress-percent"),
    uploadProgressRate: document.getElementById("upload-progress-rate"),
    uploadProgressEta: document.getElementById("upload-progress-eta"),
    jobActions: document.getElementById("job-actions"),
    jobRetry: document.getElementById("job-retry"),
    player: document.getElementById("player"),
    auth: document.getElementById("auth"),
    app: document.getElementById("app"),
    authForm: document.getElementById("auth-form"),
    authEmail: document.getElementById("auth-email"),
    authPassword: document.getElementById("auth-password"),
    authStatus: document.getElementById("auth-status"),
    authToggle: document.getElementById("auth-toggle"),
    authSubmit: document.getElementById("auth-submit"),
    who: document.getElementById("who"),
    logout: document.getElementById("logout"),
    navUpload: document.getElementById("nav-upload"),
    navLibrary: document.getElementById("nav-library"),
    navAdmin: document.getElementById("nav-admin"),
    viewUpload: document.getElementById("view-upload"),
    viewLibrary: document.getElementById("view-library"),
    viewAdmin: document.getElementById("view-admin"),
    viewEdit: document.getElementById("view-edit"),
    jobEdit: document.getElementById("job-edit"),
    libraryGrid: document.getElementById("library-grid"),
    libraryEmpty: document.getElementById("library-empty"),
    libraryStatus: document.getElementById("library-status"),
    libraryFilters: document.getElementById("library-filters"),
    libraryPrev: document.getElementById("library-prev"),
    libraryNext: document.getElementById("library-next"),
    libraryPageLabel: document.getElementById("library-page-label"),
    adminTbody: document.getElementById("admin-tbody"),
    adminEmpty: document.getElementById("admin-empty"),
    adminFilters: document.getElementById("admin-filters"),
    adminStatus: document.getElementById("admin-status"),
    adminPrev: document.getElementById("admin-prev"),
    adminNext: document.getElementById("admin-next"),
    editBack: document.getElementById("edit-back"),
    editSource: document.getElementById("edit-source"),
    editPlayer: document.getElementById("edit-player"),
    editStatus: document.getElementById("edit-status"),
    editProgress: document.getElementById("edit-progress"),
    editResult: document.getElementById("edit-result"),
    editResultName: document.getElementById("edit-result-name"),
    editResultDownload: document.getElementById("edit-result-download"),
    editResultNote: document.getElementById("edit-result-note"),
    editResultPlayer: document.getElementById("edit-result-player"),
    editForm: document.getElementById("edit-form"),
    editProcess: document.getElementById("edit-process"),
    editHint: document.getElementById("edit-hint"),
    editCrop: document.getElementById("edit-crop"),
    editCropMount: document.getElementById("edit-crop-mount"),
    editClip: document.getElementById("edit-clip"),
    editClipMount: document.getElementById("edit-clip-mount"),
    editDownscale: document.getElementById("edit-downscale"),
    editDownscaleSection: document.getElementById("edit-downscale-section"),
    editDownscaleHeight: document.getElementById("edit-downscale-height"),
    editUpscale: document.getElementById("edit-upscale"),
    editUpscaleSection: document.getElementById("edit-upscale-section"),
    editUpscaleHeight: document.getElementById("edit-upscale-height"),
    editConvert: document.getElementById("edit-convert"),
    editConvertFormat: document.getElementById("edit-convert-format"),
  };
}

function fileNamed(name = "holiday.mp4", type = "video/mp4") {
  return new File(["data"], name, { type });
}

function chooseFile(input, file = fileNamed()) {
  Object.defineProperty(input, "files", { value: [file], configurable: true });
}

const jsonResponse = (body, ok = true, status = 200) => ({
  ok,
  status,
  json: async () => body,
});

/** A storage PUT's response. The ETag is the whole point of reading it back:
 *  completion is rejected unless every part's ETag is echoed exactly. */
const partResponse = (etag, ok = true, status = 200) => ({
  ok,
  status,
  headers: { get: (name) => (name.toLowerCase() === "etag" ? etag : null) },
  json: async () => ({}),
});

/** A file whose byte length is faked, so a test can describe a large upload
 *  without allocating it. */
function fileOfSize(size, name = "holiday.mp4", type = "video/mp4") {
  const file = new File(["data"], name, { type });
  Object.defineProperty(file, "size", { value: size, configurable: true });
  return file;
}

/** Note a session down the way a reloaded page would find it. `file` is the
 *  file it was started from, so the identity the page checks comes from the
 *  same place it does. */
function rememberUpload(file, overrides = {}) {
  localStorage.setItem(
    "flickpond.pending-upload",
    JSON.stringify({
      uploadId: "up-1",
      partSize: 2,
      partCount: 3,
      userId: "u-1",
      filename: file.name,
      size: file.size,
      lastModified: file.lastModified,
      startedAt: 1,
      ...overrides,
    }),
  );
}

/** Everything a direct upload asks for *after* its session exists: one signed
 *  URL, one PUT, one completion, and a poll that answers but never advances. */
function mockUploadRest({ jobId = "job-1", etag = '"e1"' } = {}) {
  fetchMock.mockResolvedValueOnce(jsonResponse({ urls: { "1": "http://minio/part-1" } }));
  fetchMock.mockResolvedValueOnce(partResponse(etag));
  fetchMock.mockResolvedValueOnce(jsonResponse({ job_id: jobId }));
  fetchMock.mockResolvedValue(jsonResponse({ id: jobId, status: "queued" }));
}

/** A whole single-part direct upload, in the order app.js asks for it. */
function mockDirectUpload({ jobId = "job-1" } = {}) {
  fetchMock.mockResolvedValueOnce(
    jsonResponse({ upload_id: "up-1", part_size: 16777216, part_count: 1 }),
  );
  mockUploadRest({ jobId });
}

/** Select a file (via the hidden input's change event) and let the upload
 * plus first poll settle -- the dropzone's real trigger, not a form submit. */
async function uploadFile(el, file = fileNamed()) {
  chooseFile(el.fileInput, file);
  el.fileInput.dispatchEvent(new Event("change"));
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
  // jsdom has no object URLs at all. The one test that needs them adds them,
  // and this puts the environment back so every other upload test keeps
  // taking the "the browser cannot measure this file" path.
  URL.createObjectURL = undefined;
  URL.revokeObjectURL = undefined;
  // The resume note outlives a page load by design; between tests it must not.
  localStorage.clear();
});

describe("upload", () => {
  it("opens a multipart session, then PUTs the part straight to storage", async () => {
    const el = await loadApp();
    mockDirectUpload();

    await uploadFile(el);

    const [startUrl, startInit] = fetchMock.mock.calls[0];
    expect(startUrl).toBe("/api/uploads");
    expect(startInit.method).toBe("POST");
    expect(JSON.parse(startInit.body)).toEqual({
      filename: "holiday.mp4",
      size: 4,
      content_type: "video/mp4",
    });

    const [signUrl, signInit] = fetchMock.mock.calls[1];
    expect(signUrl).toBe("/api/uploads/up-1/parts");
    expect(JSON.parse(signInit.body)).toEqual({ part_numbers: [1] });

    const [putUrl, putInit] = fetchMock.mock.calls[2];
    expect(putUrl).toBe("http://minio/part-1");
    expect(putInit.method).toBe("PUT");
    // The API's session cookie has no business travelling to storage.
    expect(putInit.credentials).toBe("omit");

    const [completeUrl, completeInit] = fetchMock.mock.calls[3];
    expect(completeUrl).toBe("/api/uploads/up-1/complete");
    // The ETag goes back exactly as storage sent it, quotes and all.
    expect(JSON.parse(completeInit.body)).toEqual({ parts: [{ n: 1, etag: '"e1"' }] });
  });

  it("polls the job id the upload created", async () => {
    const el = await loadApp();
    mockDirectUpload({ jobId: "abc-123" });

    await uploadFile(el);

    expect(fetchMock.mock.calls.some(([url]) => url === "/api/jobs/abc-123")).toBe(true);
  });

  it("shows the filename and a queued badge as soon as the job is accepted", async () => {
    const el = await loadApp();
    mockDirectUpload();

    await uploadFile(el, fileNamed("holiday.mp4"));

    expect(el.activeJob.hidden).toBe(false);
    expect(el.jobFilename.textContent).toBe("holiday.mp4");
    expect(el.jobBadge.textContent).toBe("Queued");
  });

  it("ignores a second file dropped while the first upload is still in flight", async () => {
    const el = await loadApp();
    let release;
    // The session request is the one that hangs; the parts and the completion
    // after it answer normally, so the released upload can actually finish.
    fetchMock.mockReturnValueOnce(new Promise((resolve) => { release = resolve; }));
    mockUploadRest();

    chooseFile(el.fileInput, fileNamed("first.mp4"));
    el.fileInput.dispatchEvent(new Event("change"));
    await vi.waitFor(() => expect(fetchMock).toHaveBeenCalledTimes(1));
    expect(el.dropzone.classList.contains("busy")).toBe(true);

    // A second selection while the first is still pending must not double-post.
    chooseFile(el.fileInput, fileNamed("second.mp4"));
    el.fileInput.dispatchEvent(new Event("change"));
    expect(fetchMock).toHaveBeenCalledTimes(1);

    release(jsonResponse({ upload_id: "up-1", part_size: 16777216, part_count: 1 }));
    await vi.waitFor(() => expect(el.dropzone.classList.contains("busy")).toBe(false));
  });

  it("shows a percentage, a rate and a time left as the parts land", async () => {
    const el = await loadApp();
    let releaseSecond;
    let releaseThird;
    let releaseCompletion;

    fetchMock.mockResolvedValueOnce(jsonResponse({ upload_id: "up-1", part_size: 2, part_count: 3 }));
    fetchMock.mockResolvedValueOnce(jsonResponse({ urls: { "1": "http://minio/p1" } }));
    fetchMock.mockResolvedValueOnce(partResponse('"e1"'));
    fetchMock.mockReturnValueOnce(
      new Promise((resolve) => {
        releaseSecond = () => resolve(jsonResponse({ urls: { "2": "http://minio/p2" } }));
      }),
    );
    fetchMock.mockResolvedValueOnce(partResponse('"e2"'));
    fetchMock.mockReturnValueOnce(
      new Promise((resolve) => {
        releaseThird = () => resolve(jsonResponse({ urls: { "3": "http://minio/p3" } }));
      }),
    );
    fetchMock.mockResolvedValueOnce(partResponse('"e3"'));
    fetchMock.mockReturnValueOnce(
      new Promise((resolve) => {
        releaseCompletion = () => resolve(jsonResponse({ job_id: "job-1" }));
      }),
    );
    fetchMock.mockResolvedValue(jsonResponse({ id: "job-1", status: "queued" }));

    // Six real bytes, so the three 2-byte parts are really 2 bytes each.
    chooseFile(el.fileInput, new File(["abcdef"], "holiday.mp4", { type: "video/mp4" }));
    el.fileInput.dispatchEvent(new Event("change"));
    await vi.advanceTimersByTimeAsync(0);

    // Two of six bytes up: a third, and one measurement is not yet a rate.
    expect(el.uploadProgress.hidden).toBe(false);
    expect(el.uploadProgressPercent.textContent).toBe("33%");
    expect(el.uploadProgressFill.style.width).toBe("33%");
    expect(el.uploadProgressTrack.getAttribute("aria-valuenow")).toBe("33");
    expect(el.uploadProgressRate.textContent).toBe("");
    expect(el.uploadProgressEta.textContent).toBe("");

    // Two seconds later the second part lands, so the window has a rate.
    await vi.advanceTimersByTimeAsync(2000);
    releaseSecond();
    await vi.advanceTimersByTimeAsync(0);

    expect(el.uploadProgressPercent.textContent).toBe("67%");
    expect(el.uploadProgressRate.textContent).toBe("1 B/s");
    expect(el.uploadProgressEta.textContent).toBe("about 2s left");

    await vi.advanceTimersByTimeAsync(2000);
    releaseThird();
    await vi.advanceTimersByTimeAsync(0);

    expect(el.uploadProgressPercent.textContent).toBe("100%");
    expect(el.uploadProgressEta.textContent).toBe("almost done");

    // The bar belongs to the transfer, so it goes when the transfer does.
    releaseCompletion();
    await vi.advanceTimersByTimeAsync(0);
    expect(el.uploadProgress.hidden).toBe(true);
    expect(el.jobBadge.textContent).toBe("Queued");
  });

  it("falls back to the one-shot POST when the server has no direct upload", async () => {
    const el = await loadApp();
    fetchMock.mockResolvedValueOnce(jsonResponse({ error: "not found" }, false, 404));
    fetchMock.mockResolvedValueOnce(jsonResponse({ job_id: "job-1" }));
    fetchMock.mockResolvedValue(jsonResponse({ id: "job-1", status: "queued" }));

    await uploadFile(el);

    const [url, init] = fetchMock.mock.calls[1];
    expect(url).toBe("/api/upload");
    expect(init.body.get("file")).toBeInstanceOf(File);
    expect(el.jobBadge.textContent).toBe("Queued");
  });

  it("takes a file past the legacy limit when the direct path is available", async () => {
    const el = await loadApp();
    mockDirectUpload();

    await uploadFile(el, fileOfSize(101 * 1024 * 1024));

    // The 100 MB ceiling belongs to the one-shot endpoint, not to this path.
    expect(JSON.parse(fetchMock.mock.calls[0][1].body).size).toBe(101 * 1024 * 1024);
    expect(el.status.textContent).not.toContain("larger than");
  });

  it("refuses a large file that neither path can take", async () => {
    const el = await loadApp();

    // No declared type: the direct path cannot name one, and the one-shot
    // endpoint stops at 100 MB.
    chooseFile(el.fileInput, fileOfSize(101 * 1024 * 1024, "raw.mkv", ""));
    el.fileInput.dispatchEvent(new Event("change"));
    await vi.advanceTimersByTimeAsync(0);

    expect(el.status.textContent).toContain("larger than");
    expect(fetchMock).not.toHaveBeenCalled();
    expect(el.dropzone.classList.contains("busy")).toBe(false);
  });

  it("says the duration limit the API reports, next to the picker", async () => {
    const el = await loadApp({ limits: { max_duration_seconds: 300, max_edit_height: 2160 } });

    expect(el.dropzoneHint.textContent).toBe("MP4, MOV, WebM, MKV, or AVI, up to 5 minutes.");
  });

  it("refuses an over-long video before spending the upload on it", async () => {
    const el = await loadApp({ limits: { max_duration_seconds: 300, max_edit_height: 2160 } });

    // The browser half of this check is a <video> reading the file's own
    // metadata. jsdom has no demuxer, so the element is stood in for: what is
    // under test is what the page does with the number, not how it got it.
    URL.createObjectURL = () => "blob:probe";
    URL.revokeObjectURL = () => {};
    const listeners = {};
    const probe = document.createElement("video");
    Object.defineProperty(probe, "duration", { value: 754.2, configurable: true });
    probe.addEventListener = (name, handler) => {
      listeners[name] = handler;
    };
    const realCreate = document.createElement.bind(document);
    let served = false;
    vi.spyOn(document, "createElement").mockImplementation((tag, ...rest) => {
      if (tag === "video" && !served) {
        served = true;
        return probe;
      }
      return realCreate(tag, ...rest);
    });

    chooseFile(el.fileInput, fileNamed("long.mp4"));
    el.fileInput.dispatchEvent(new Event("change"));
    listeners.loadedmetadata();
    await vi.advanceTimersByTimeAsync(0);

    // 12:34 against a 5 minute limit, and nothing was sent to find that out.
    expect(el.status.textContent).toContain("longer than 5 minutes");
    expect(fetchMock).not.toHaveBeenCalled();
    expect(el.dropzone.classList.contains("busy")).toBe(false);
    expect(el.uploadProgress.hidden).toBe(true);
  });

  it("names the CORS rule when storage does not expose the ETag", async () => {
    const el = await loadApp();
    fetchMock.mockResolvedValueOnce(jsonResponse({ upload_id: "up-1", part_size: 2, part_count: 1 }));
    fetchMock.mockResolvedValueOnce(jsonResponse({ urls: { "1": "http://minio/p1" } }));
    fetchMock.mockResolvedValueOnce(partResponse(null));

    await uploadFile(el);

    expect(el.status.textContent).toContain("ETag");
    expect(el.dropzone.classList.contains("busy")).toBe(false);
    expect(el.uploadProgress.hidden).toBe(true);
  });

  it("surfaces the API error message and clears the busy state", async () => {
    const el = await loadApp();
    fetchMock.mockResolvedValueOnce(jsonResponse({ error: "file too large" }, false, 400));

    await uploadFile(el);

    expect(el.status.textContent).toContain("file too large");
    expect(el.dropzone.classList.contains("busy")).toBe(false);
    expect(fetchMock).toHaveBeenCalledTimes(1);
  });

  it("falls back to the status code when the error body is unreadable", async () => {
    const el = await loadApp();
    fetchMock.mockResolvedValueOnce({
      ok: false,
      status: 500,
      json: async () => {
        throw new Error("not json");
      },
    });

    await uploadFile(el);

    expect(el.status.textContent).toContain("500");
  });

  it("reports a network failure rather than hanging", async () => {
    const el = await loadApp();
    fetchMock.mockRejectedValueOnce(new Error("connection refused"));

    await uploadFile(el);

    expect(el.status.textContent).toContain("connection refused");
    expect(el.dropzone.classList.contains("busy")).toBe(false);
  });

  it("clears the previous job's player when a second upload starts", async () => {
    const el = await loadApp();
    mockDirectUpload();
    fetchMock.mockResolvedValue(jsonResponse({ status: "done", output_url: "http://minio/a.mp4" }));
    await uploadFile(el);
    expect(el.player.hidden).toBe(false);

    fetchMock.mockResolvedValue(jsonResponse({ id: "job-2", status: "queued" }));
    mockDirectUpload({ jobId: "job-2" });
    await uploadFile(el);

    expect(el.player.hidden).toBe(true);
    expect(el.player.querySelector("video")).toBeNull();
    expect(el.jobBadge.textContent).toBe("Queued");
  });

  it("rejects an unsupported type before ever calling the API", async () => {
    const el = await loadApp();

    chooseFile(el.fileInput, fileNamed("notes.pdf", "application/pdf"));
    el.fileInput.dispatchEvent(new Event("change"));

    expect(el.status.textContent).toContain("not a video format");
    expect(fetchMock).not.toHaveBeenCalled();
  });

  // --- deleting the active job straight off the upload page ---------------

  it("asks for confirmation before deleting the active job, and does nothing if declined", async () => {
    const el = await loadApp();
    fetchMock.mockResolvedValue(jsonResponse({ job_id: "job-1", status: "queued" }));
    await uploadFile(el);
    vi.spyOn(window, "confirm").mockReturnValue(false);
    fetchMock.mockClear();

    el.jobDelete.dispatchEvent(new Event("click"));

    expect(window.confirm).toHaveBeenCalledOnce();
    expect(fetchMock).not.toHaveBeenCalled();
    expect(el.activeJob.hidden).toBe(false);
  });

  it("deletes the active job and resets the card once confirmed", async () => {
    const el = await loadApp();
    fetchMock.mockResolvedValue(jsonResponse({ job_id: "job-1", status: "queued" }));
    await uploadFile(el);
    vi.spyOn(window, "confirm").mockReturnValue(true);
    fetchMock.mockClear();
    fetchMock.mockResolvedValueOnce({ ok: true, status: 204, json: async () => ({}) });

    el.jobDelete.dispatchEvent(new Event("click"));
    await vi.waitFor(() => expect(fetchMock).toHaveBeenCalled());
    await vi.advanceTimersByTimeAsync(0);

    const [url, init] = fetchMock.mock.calls[0];
    expect(url).toBe("/api/jobs/job-1");
    expect(init.method).toBe("DELETE");
    expect(el.activeJob.hidden).toBe(true);
    expect(el.status.textContent).toBe("Video deleted.");
  });

  it("stops polling once the active job is deleted", async () => {
    const el = await loadApp();
    mockDirectUpload();
    fetchMock.mockResolvedValue(jsonResponse({ id: "job-1", status: "processing" }));
    await uploadFile(el);
    vi.spyOn(window, "confirm").mockReturnValue(true);
    fetchMock.mockClear();
    fetchMock.mockResolvedValueOnce({ ok: true, status: 204, json: async () => ({}) });

    el.jobDelete.dispatchEvent(new Event("click"));
    await vi.waitFor(() => expect(fetchMock).toHaveBeenCalled());
    await vi.advanceTimersByTimeAsync(0);
    fetchMock.mockClear();

    await vi.advanceTimersByTimeAsync(10000);

    expect(fetchMock).not.toHaveBeenCalled();
  });

  it("returns to the sign-in form if the session expired mid-delete", async () => {
    const el = await loadApp();
    fetchMock.mockResolvedValue(jsonResponse({ job_id: "job-1", status: "queued" }));
    await uploadFile(el);
    vi.spyOn(window, "confirm").mockReturnValue(true);
    fetchMock.mockClear();
    fetchMock.mockResolvedValueOnce(jsonResponse({ error: "not authenticated" }, false, 401));

    el.jobDelete.dispatchEvent(new Event("click"));
    await vi.waitFor(() => expect(fetchMock).toHaveBeenCalled());
    await vi.advanceTimersByTimeAsync(0);

    expect(el.auth.hidden).toBe(false);
    expect(el.authStatus.textContent).toContain("session expired");
  });

  it("opens the file picker from the dropzone, once per click", async () => {
    const el = await loadApp();
    const picker = vi.spyOn(el.fileInput, "click").mockImplementation(() => {});

    el.dropzone.dispatchEvent(new Event("click"));
    expect(picker).toHaveBeenCalledTimes(1);

    // The browse button sits inside the dropzone, so its click reaches both
    // handlers -- which must still be one trip to the picker, not two.
    el.browseButton.dispatchEvent(new Event("click", { bubbles: true }));
    expect(picker).toHaveBeenCalledTimes(2);
  });

  it("takes a dropped file, and ignores a drop carrying none", async () => {
    const el = await loadApp();
    fetchMock.mockResolvedValue(jsonResponse({ job_id: "job-1", status: "queued" }));

    el.dropzone.dispatchEvent(new Event("dragover"));
    expect(el.dropzone.classList.contains("over")).toBe(true);
    el.dropzone.dispatchEvent(new Event("dragleave"));
    expect(el.dropzone.classList.contains("over")).toBe(false);

    const empty = new Event("drop");
    empty.dataTransfer = { files: [] };
    el.dropzone.dispatchEvent(empty);
    expect(fetchMock).not.toHaveBeenCalled();

    const withFile = new Event("drop");
    withFile.dataTransfer = { files: [fileNamed()] };
    el.dropzone.dispatchEvent(withFile);
    await vi.waitFor(() => expect(fetchMock).toHaveBeenCalled());
  });

  it("returns to the sign-in form if the session expired during the upload", async () => {
    const el = await loadApp();
    fetchMock.mockResolvedValueOnce(jsonResponse({ error: "not authenticated" }, false, 401));

    await uploadFile(el);

    expect(el.auth.hidden).toBe(false);
    expect(el.authStatus.textContent).toContain("session expired");
    expect(el.dropzone.classList.contains("busy")).toBe(false);
  });

  it("does nothing when there is no active job to delete", async () => {
    const el = await loadApp();
    vi.spyOn(window, "confirm").mockReturnValue(true);

    el.jobDelete.dispatchEvent(new Event("click"));

    expect(window.confirm).not.toHaveBeenCalled();
    expect(fetchMock).not.toHaveBeenCalled();
  });

  it("keeps the card and says why when the delete is refused", async () => {
    const el = await loadApp();
    fetchMock.mockResolvedValue(jsonResponse({ job_id: "job-1", status: "queued" }));
    await uploadFile(el);
    vi.spyOn(window, "confirm").mockReturnValue(true);
    fetchMock.mockClear();
    fetchMock.mockResolvedValueOnce(jsonResponse({ error: "not found" }, false, 404));

    el.jobDelete.dispatchEvent(new Event("click"));
    await vi.waitFor(() => expect(fetchMock).toHaveBeenCalled());
    await vi.advanceTimersByTimeAsync(0);

    expect(el.status.textContent).toContain("not found");
    expect(el.activeJob.hidden).toBe(false);
  });

  it("reports a network failure while deleting rather than hanging", async () => {
    const el = await loadApp();
    fetchMock.mockResolvedValue(jsonResponse({ job_id: "job-1", status: "queued" }));
    await uploadFile(el);
    vi.spyOn(window, "confirm").mockReturnValue(true);
    fetchMock.mockClear();
    fetchMock.mockRejectedValueOnce(new Error("connection refused"));

    el.jobDelete.dispatchEvent(new Event("click"));
    await vi.waitFor(() => expect(fetchMock).toHaveBeenCalled());
    await vi.advanceTimersByTimeAsync(0);

    expect(el.status.textContent).toContain("connection refused");
  });

  it("re-runs the last file from the retry button, and does nothing before one exists", async () => {
    const el = await loadApp();

    el.jobRetry.dispatchEvent(new Event("click"));
    expect(fetchMock).not.toHaveBeenCalled();

    fetchMock.mockResolvedValue(jsonResponse({ job_id: "job-1", status: "queued" }));
    await uploadFile(el);
    fetchMock.mockClear();
    fetchMock.mockResolvedValue(jsonResponse({ job_id: "job-2", status: "queued" }));

    el.jobRetry.dispatchEvent(new Event("click"));
    await vi.waitFor(() => expect(fetchMock).toHaveBeenCalled());
    await vi.advanceTimersByTimeAsync(0);

    expect(fetchMock.mock.calls.some(([url]) => url === "/api/upload")).toBe(true);
  });
});

describe("resuming an upload", () => {
  it("offers to finish an upload that was interrupted", async () => {
    const file = new File(["abcdef"], "holiday.mp4", { type: "video/mp4" });
    rememberUpload(file);

    const el = await loadApp();

    expect(el.resumePrompt.hidden).toBe(false);
    expect(el.resumeText.textContent).toContain("holiday.mp4");
  });

  it("asks the server what it has, and sends only the missing parts", async () => {
    const file = new File(["abcdef"], "holiday.mp4", { type: "video/mp4" });
    rememberUpload(file);
    const el = await loadApp();

    fetchMock.mockResolvedValueOnce(
      jsonResponse({
        parts_done: [{ n: 1, etag: '"e1"' }],
        state: "open",
        part_size: 2,
        part_count: 3,
        job_id: null,
      }),
    );
    fetchMock.mockResolvedValueOnce(jsonResponse({ urls: { "2": "http://minio/p2" } }));
    fetchMock.mockResolvedValueOnce(partResponse('"e2"'));
    fetchMock.mockResolvedValueOnce(jsonResponse({ urls: { "3": "http://minio/p3" } }));
    fetchMock.mockResolvedValueOnce(partResponse('"e3"'));
    fetchMock.mockResolvedValueOnce(jsonResponse({ job_id: "job-1" }));
    fetchMock.mockResolvedValue(jsonResponse({ id: "job-1", status: "queued" }));

    await uploadFile(el, file);

    // The server is the authority on what is already in storage.
    expect(fetchMock.mock.calls[0][0]).toBe("/api/uploads/up-1");

    const signed = fetchMock.mock.calls
      .filter(([url]) => url === "/api/uploads/up-1/parts")
      .map(([, init]) => JSON.parse(init.body).part_numbers);
    expect(signed).toEqual([[2], [3]]);

    // Completion takes the whole manifest, the part already in storage too.
    const complete = fetchMock.mock.calls.find(([url]) => url === "/api/uploads/up-1/complete");
    expect(JSON.parse(complete[1].body).parts).toEqual([
      { n: 1, etag: '"e1"' },
      { n: 2, etag: '"e2"' },
      { n: 3, etag: '"e3"' },
    ]);

    // There is a job now, so there is nothing left to come back to.
    expect(localStorage.getItem("flickpond.pending-upload")).toBeNull();
    expect(el.jobBadge.textContent).toBe("Queued");
  });

  it("starts over when the server no longer has the session", async () => {
    const file = new File(["abcdef"], "holiday.mp4", { type: "video/mp4" });
    rememberUpload(file);
    const el = await loadApp();

    fetchMock.mockResolvedValueOnce(jsonResponse({ error: "not found" }, false, 404));
    fetchMock.mockResolvedValueOnce(
      jsonResponse({ upload_id: "up-2", part_size: 2, part_count: 1 }),
    );
    mockUploadRest();

    await uploadFile(el, file);

    // It asked, was told there was nothing, and opened a new session.
    expect(fetchMock.mock.calls[0][0]).toBe("/api/uploads/up-1");
    expect(fetchMock.mock.calls[1][0]).toBe("/api/uploads");
    expect(JSON.parse(fetchMock.mock.calls[1][1].body).size).toBe(6);
  });

  it("does not offer one account's unfinished upload to another", async () => {
    const file = new File(["abcdef"], "holiday.mp4", { type: "video/mp4" });
    rememberUpload(file, { userId: "someone-else" });
    const el = await loadApp();

    expect(el.resumePrompt.hidden).toBe(true);

    mockDirectUpload();
    await uploadFile(el, file);

    // A fresh session rather than someone else's.
    expect(fetchMock.mock.calls[0][0]).toBe("/api/uploads");
  });

  it("lets the reader abandon an unfinished upload", async () => {
    const file = new File(["abcdef"], "holiday.mp4", { type: "video/mp4" });
    rememberUpload(file);
    const el = await loadApp();
    fetchMock.mockResolvedValueOnce({ ok: true, status: 204, json: async () => ({}) });

    el.resumeDiscard.dispatchEvent(new Event("click"));
    await vi.waitFor(() => expect(fetchMock).toHaveBeenCalled());
    await vi.advanceTimersByTimeAsync(0);

    expect(fetchMock.mock.calls[0]).toEqual(["/api/uploads/up-1", { method: "DELETE" }]);
    expect(el.resumePrompt.hidden).toBe(true);
    expect(localStorage.getItem("flickpond.pending-upload")).toBeNull();
  });

  it("uploads anyway when the browser refuses to keep anything", async () => {
    const el = await loadApp();
    mockDirectUpload();
    const refuse = () => {
      throw new Error("storage disabled");
    };
    vi.spyOn(Storage.prototype, "getItem").mockImplementation(refuse);
    vi.spyOn(Storage.prototype, "setItem").mockImplementation(refuse);
    vi.spyOn(Storage.prototype, "removeItem").mockImplementation(refuse);

    await uploadFile(el);

    // Having nowhere to write the note is not a reason to refuse the upload.
    expect(el.jobBadge.textContent).toBe("Queued");
    expect(el.resumePrompt.hidden).toBe(true);
  });

  it("reopens the picker from the prompt", async () => {
    const file = new File(["abcdef"], "holiday.mp4", { type: "video/mp4" });
    rememberUpload(file);
    const el = await loadApp();
    const clicked = vi.spyOn(el.fileInput, "click").mockImplementation(() => {});

    el.resumeChoose.dispatchEvent(new Event("click"));

    expect(clicked).toHaveBeenCalled();
  });
});

describe("polling", () => {
  // Every upload makes four requests before its first poll: the session, the
  // part's signed URL, the part itself, and the completion.
  const UPLOAD_CALLS = 4;

  /** Upload, then have every poll return the given job document. */
  async function pollReturning(job) {
    const el = await loadApp();
    mockDirectUpload();
    fetchMock.mockResolvedValue(jsonResponse(job));
    await uploadFile(el);
    return el;
  }

  it("shows the player and stops polling once the job is done", async () => {
    const el = await pollReturning({ status: "done", output_url: "http://minio/out.mp4" });

    expect(el.player.querySelector("video").getAttribute("src")).toBe("http://minio/out.mp4");
    expect(el.player.hidden).toBe(false);
    expect(el.jobBadge.textContent).toBe("Done");
    await vi.advanceTimersByTimeAsync(10000);
    expect(fetchMock).toHaveBeenCalledTimes(UPLOAD_CALLS + 1);
  });

  it("shows the error, offers a retry, and stops polling once the job has failed", async () => {
    const el = await pollReturning({ status: "failed", error: "source missing" });

    expect(el.status.textContent).toContain("source missing");
    expect(el.player.hidden).toBe(true);
    expect(el.jobActions.hidden).toBe(false);
    await vi.advanceTimersByTimeAsync(10000);
    expect(fetchMock).toHaveBeenCalledTimes(UPLOAD_CALLS + 1);
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
    await vi.advanceTimersByTimeAsync(10000);
    // 4 upload calls + 1 immediate poll + 5 more at 2s intervals over the next 10s.
    expect(fetchMock).toHaveBeenCalledTimes(UPLOAD_CALLS + 6);
  });

  it("shows the progress sweep only while the job is processing", async () => {
    const el = await pollReturning({ status: "processing" });

    expect(el.jobProgress.hidden).toBe(false);
  });

  it("says HD is still coming while the ladder is being built", async () => {
    const el = await loadApp();
    mockDirectUpload();
    // Done and playable, with the ladder still in flight -- exactly the state
    // `hls_status` exists to describe, and one a missing `hls_url` cannot tell
    // apart from a ladder that is never coming.
    fetchMock.mockResolvedValue(
      jsonResponse({
        id: "job-1",
        status: "done",
        output_url: "http://minio/out.mp4",
        hls_status: "pending",
      }),
    );

    await uploadFile(el);

    expect(el.status.textContent).toBe("Ready to play — HD versions are still processing.");
    // Playable now, rather than after the ladder: that is the whole point of
    // the ladder running as its own job.
    expect(el.player.hidden).toBe(false);
  });

  // --- a job that never finishes (P4) -------------------------------------

  /** 30 polls at 2s: the point where the page starts saying it is slow. */
  const SLOW_AFTER_MS = 30 * 2000;

  it("says so once a job has been processing far longer than it should", async () => {
    const el = await pollReturning({ status: "processing" });
    expect(el.status.textContent).toBe("Processing...");

    await vi.advanceTimersByTimeAsync(SLOW_AFTER_MS);

    expect(el.status.textContent).toContain("longer than expected");
    expect(el.status.textContent).toContain("job-1");
  });

  it("hands the form back so a stuck job does not lock the page", async () => {
    const el = await pollReturning({ status: "processing" });
    expect(el.jobActions.hidden).toBe(true);

    await vi.advanceTimersByTimeAsync(SLOW_AFTER_MS);

    expect(el.jobActions.hidden).toBe(false);
  });

  it("keeps polling after saying it is slow, because slow is not failed", async () => {
    const el = await pollReturning({ status: "processing" });
    await vi.advanceTimersByTimeAsync(SLOW_AFTER_MS);
    const before = fetchMock.mock.calls.length;

    await vi.advanceTimersByTimeAsync(2000);

    expect(fetchMock.mock.calls.length).toBe(before + 1);
    expect(el.status.textContent).not.toContain("failed");
  });

  it("recovers normally if a long-running job does eventually finish", async () => {
    const el = await pollReturning({ status: "processing" });
    await vi.advanceTimersByTimeAsync(SLOW_AFTER_MS);
    expect(el.status.textContent).toContain("longer than expected");

    fetchMock.mockResolvedValue(jsonResponse({ status: "done", output_url: "http://minio/late.mp4" }));
    await vi.advanceTimersByTimeAsync(2000);

    expect(el.status.textContent).toBe("Processing complete.");
    expect(el.player.querySelector("video").getAttribute("src")).toBe("http://minio/late.mp4");
  });

  it("shows an unrecognised status verbatim rather than blanking the UI", async () => {
    const el = await pollReturning({ status: "cancelled" });

    expect(el.status.textContent).toBe("cancelled");
  });

  it("a new upload cancels the previous job's scheduled poll", async () => {
    const el = await pollReturning({ status: "queued" });
    const afterFirst = fetchMock.mock.calls.length;

    fetchMock.mockResolvedValue(jsonResponse({ job_id: "job-2", status: "done" }));
    await uploadFile(el);
    const afterSecond = fetchMock.mock.calls.length;
    await vi.advanceTimersByTimeAsync(10000);

    expect(fetchMock.mock.calls.length).toBe(afterSecond);
    expect(afterSecond).toBeGreaterThan(afterFirst);
  });
});

describe("signing in", () => {
  it("shows the sign-in form and hides the app when not signed in", async () => {
    const el = await loadApp({ signedIn: false });

    expect(el.auth.hidden).toBe(false);
    expect(el.app.hidden).toBe(true);
  });

  it("shows the app and the signed-in address when /auth/me answers", async () => {
    const el = await loadApp();

    expect(el.auth.hidden).toBe(true);
    expect(el.app.hidden).toBe(false);
    expect(el.who.textContent).toBe("maya@example.test");
  });

  it("posts credentials as JSON and reveals the app on success", async () => {
    const el = await loadApp({ signedIn: false });
    el.authEmail.value = "maya@example.test";
    el.authPassword.value = "hunter2hunter2";
    fetchMock.mockResolvedValueOnce(
      jsonResponse({ id: "u-1", email: "maya@example.test", role: "user" }),
    );

    el.authForm.dispatchEvent(new Event("submit", { cancelable: true }));
    await vi.waitFor(() => expect(fetchMock).toHaveBeenCalled());
    await vi.advanceTimersByTimeAsync(0);

    const [url, init] = fetchMock.mock.calls[0];
    expect(url).toBe("/api/auth/login");
    expect(init.method).toBe("POST");
    expect(JSON.parse(init.body)).toEqual({
      email: "maya@example.test",
      password: "hunter2hunter2",
    });
    expect(el.app.hidden).toBe(false);
  });

  it("never attaches a token, because it cannot read one", async () => {
    // The session cookie is HttpOnly. If a future change starts putting an
    // Authorization header on here, the token has come from somewhere a script
    // can read -- which is the thing this design exists to prevent.
    const el = await loadApp({ signedIn: false });
    el.authEmail.value = "maya@example.test";
    el.authPassword.value = "hunter2hunter2";
    fetchMock.mockResolvedValueOnce(
      jsonResponse({ id: "u-1", email: "maya@example.test", role: "user" }),
    );

    el.authForm.dispatchEvent(new Event("submit", { cancelable: true }));
    await vi.waitFor(() => expect(fetchMock).toHaveBeenCalled());

    for (const [, init] of fetchMock.mock.calls) {
      expect(init?.headers?.Authorization).toBeUndefined();
    }
  });

  it("does not say which half of the credentials was wrong", async () => {
    const el = await loadApp({ signedIn: false });
    el.authEmail.value = "maya@example.test";
    el.authPassword.value = "wrongwrongwrong";
    fetchMock.mockResolvedValueOnce(jsonResponse({ error: "invalid" }, false, 401));

    el.authForm.dispatchEvent(new Event("submit", { cancelable: true }));
    await vi.waitFor(() => expect(fetchMock).toHaveBeenCalled());
    await vi.advanceTimersByTimeAsync(0);

    expect(el.authStatus.textContent).toBe("Incorrect email or password.");
    expect(el.app.hidden).toBe(true);
  });

  it("reports a taken address on register", async () => {
    const el = await loadApp({ signedIn: false });
    el.authToggle.dispatchEvent(new Event("click"));
    el.authEmail.value = "taken@example.test";
    el.authPassword.value = "hunter2hunter2";
    fetchMock.mockResolvedValueOnce(jsonResponse({}, false, 409));

    el.authForm.dispatchEvent(new Event("submit", { cancelable: true }));
    await vi.waitFor(() => expect(fetchMock).toHaveBeenCalled());
    await vi.advanceTimersByTimeAsync(0);

    expect(fetchMock.mock.calls[0][0]).toBe("/api/auth/register");
    expect(el.authStatus.textContent).toContain("already registered");
  });

  it("signs out through the server, because a script cannot clear the cookie", async () => {
    const el = await loadApp();
    fetchMock.mockResolvedValueOnce(jsonResponse({}, true, 204));

    el.logout.dispatchEvent(new Event("click"));
    await vi.waitFor(() => expect(fetchMock).toHaveBeenCalled());
    await vi.advanceTimersByTimeAsync(0);

    const [url, init] = fetchMock.mock.calls[0];
    expect(url).toBe("/api/auth/logout");
    expect(init.method).toBe("POST");
    expect(el.auth.hidden).toBe(false);
    expect(el.app.hidden).toBe(true);
  });

  it("returns to the sign-in form when a session expires mid-poll", async () => {
    const el = await loadApp();
    fetchMock
      .mockResolvedValueOnce(jsonResponse({ job_id: "job-1" }))
      .mockResolvedValue(jsonResponse({ error: "not authenticated" }, false, 401));

    await uploadFile(el);

    expect(el.auth.hidden).toBe(false);
    expect(el.authStatus.textContent).toContain("session expired");
  });

  it("hides the admin tab for an ordinary user", async () => {
    const el = await loadApp({ role: "user" });

    expect(el.navAdmin.hidden).toBe(true);
  });

  it("shows the admin tab for an operator", async () => {
    const el = await loadApp({ role: "operator" });

    expect(el.navAdmin.hidden).toBe(false);
  });

  it("asks for a valid address and a long enough password on 422", async () => {
    const el = await loadApp({ signedIn: false });
    el.authEmail.value = "not-an-address";
    el.authPassword.value = "short";
    fetchMock.mockResolvedValueOnce(jsonResponse({ detail: [] }, false, 422));

    el.authForm.dispatchEvent(new Event("submit", { cancelable: true }));
    await vi.waitFor(() => expect(fetchMock).toHaveBeenCalled());
    await vi.advanceTimersByTimeAsync(0);

    expect(el.authStatus.textContent).toContain("at least 8 characters");
  });

  it("reports a status the sign-in form has no wording for", async () => {
    const el = await loadApp({ signedIn: false });
    el.authEmail.value = "maya@example.test";
    el.authPassword.value = "hunter2hunter2";
    fetchMock.mockResolvedValueOnce(jsonResponse({ error: "boom" }, false, 500));

    el.authForm.dispatchEvent(new Event("submit", { cancelable: true }));
    await vi.waitFor(() => expect(fetchMock).toHaveBeenCalled());
    await vi.advanceTimersByTimeAsync(0);

    expect(el.authStatus.textContent).toContain("500");
  });

  it("reports a network failure on sign-in instead of hanging", async () => {
    const el = await loadApp({ signedIn: false });
    el.authEmail.value = "maya@example.test";
    el.authPassword.value = "hunter2hunter2";
    fetchMock.mockRejectedValueOnce(new Error("connection refused"));

    el.authForm.dispatchEvent(new Event("submit", { cancelable: true }));
    await vi.waitFor(() => expect(fetchMock).toHaveBeenCalled());
    await vi.advanceTimersByTimeAsync(0);

    expect(el.authStatus.textContent).toContain("connection refused");
    // The button has to come back, or the form is dead after one failure.
    expect(el.authSubmit.disabled).toBe(false);
  });

  it("shows the sign-in form when the API cannot be reached at all", async () => {
    fetchMock.mockRejectedValueOnce(new Error("connection refused"));

    const el = await loadApp();

    expect(el.auth.hidden).toBe(false);
    expect(el.app.hidden).toBe(true);
  });

  it("refuses the admin view for an ordinary user, however it is asked for", async () => {
    const el = await loadApp({ role: "user" });

    // The tab is hidden, not disabled: jsdom clicks it happily, and a future
    // deep link would arrive the same way.
    el.navAdmin.dispatchEvent(new Event("click"));

    expect(el.viewAdmin.hidden).toBe(true);
    expect(el.viewUpload.hidden).toBe(false);
  });
});

describe("library", () => {
  const JOBS = [
    { id: "1", filename: "holiday.mp4", status: "done", output_url: "http://minio/holiday.mp4" },
    { id: "2", filename: "recap.mov", status: "processing" },
    { id: "3", filename: "broken.mkv", status: "failed", error: "corrupt file" },
  ];

  it("fetches the caller's own jobs when the tab is opened", async () => {
    const el = await loadApp();
    fetchMock.mockResolvedValueOnce(jsonResponse(JOBS));

    el.navLibrary.dispatchEvent(new Event("click"));
    await vi.waitFor(() => expect(fetchMock).toHaveBeenCalled());
    await vi.advanceTimersByTimeAsync(0);

    expect(fetchMock.mock.calls[0][0]).toBe("/api/jobs?limit=12&offset=0");
    expect(el.viewLibrary.hidden).toBe(false);
    expect(el.viewUpload.hidden).toBe(true);
  });

  it("renders one card per job, named by filename", async () => {
    const el = await loadApp();
    fetchMock.mockResolvedValueOnce(jsonResponse(JOBS));

    el.navLibrary.dispatchEvent(new Event("click"));
    await vi.waitFor(() => expect(fetchMock).toHaveBeenCalled());
    await vi.advanceTimersByTimeAsync(0);

    const names = [...el.libraryGrid.querySelectorAll(".video-filename")].map((n) => n.textContent);
    expect(names).toEqual(["holiday.mp4", "recap.mov", "broken.mkv"]);
  });

  it("shows the poster frame the API handed back, behind the badge and overlay", async () => {
    const el = await loadApp();
    fetchMock.mockResolvedValueOnce(
      jsonResponse([{ ...JOBS[0], thumbnail_url: "http://minio/thumbs/1.jpg" }]),
    );

    el.navLibrary.dispatchEvent(new Event("click"));
    await vi.waitFor(() => expect(fetchMock).toHaveBeenCalled());
    await vi.advanceTimersByTimeAsync(0);

    const frame = el.libraryGrid.querySelector(".thumb-image");
    expect(frame.getAttribute("src")).toBe("http://minio/thumbs/1.jpg");
    // Decorative: the filename below it already names the video.
    expect(frame.getAttribute("alt")).toBe("");
    expect(frame.getAttribute("loading")).toBe("lazy");
    // A sibling inside the thumb, so the badge and the play overlay survive.
    expect(frame.closest(".video-thumb").querySelector(".badge")).not.toBeNull();
  });

  it("keeps the placeholder for a job with no poster frame yet", async () => {
    const el = await loadApp();
    fetchMock.mockResolvedValueOnce(jsonResponse(JOBS));

    el.navLibrary.dispatchEvent(new Event("click"));
    await vi.waitFor(() => expect(fetchMock).toHaveBeenCalled());
    await vi.advanceTimersByTimeAsync(0);

    expect(el.libraryGrid.querySelectorAll(".video-thumb")).toHaveLength(3);
    expect(el.libraryGrid.querySelector(".thumb-image")).toBeNull();
  });

  it("falls back to the placeholder when a poster frame will not load", async () => {
    const el = await loadApp();
    fetchMock.mockResolvedValueOnce(
      jsonResponse([{ ...JOBS[0], thumbnail_url: "http://minio/thumbs/expired.jpg" }]),
    );

    el.navLibrary.dispatchEvent(new Event("click"));
    await vi.waitFor(() => expect(fetchMock).toHaveBeenCalled());
    await vi.advanceTimersByTimeAsync(0);

    el.libraryGrid.querySelector(".thumb-image").dispatchEvent(new Event("error"));

    expect(el.libraryGrid.querySelector(".thumb-image")).toBeNull();
    // Only the frame goes: the card, its badge and its name are untouched.
    expect(el.libraryGrid.querySelector(".video-thumb .badge")).not.toBeNull();
    expect(el.libraryGrid.querySelector(".video-filename").textContent).toBe("holiday.mp4");
  });

  it("shows the empty state when there are no jobs at all", async () => {
    const el = await loadApp();
    fetchMock.mockResolvedValueOnce(jsonResponse([]));

    el.navLibrary.dispatchEvent(new Event("click"));
    await vi.waitFor(() => expect(fetchMock).toHaveBeenCalled());
    await vi.advanceTimersByTimeAsync(0);

    expect(el.libraryEmpty.hidden).toBe(false);
    expect(el.libraryGrid.hidden).toBe(true);
  });

  it("filters client-side, with no extra request", async () => {
    const el = await loadApp();
    fetchMock.mockResolvedValueOnce(jsonResponse(JOBS));
    el.navLibrary.dispatchEvent(new Event("click"));
    await vi.waitFor(() => expect(fetchMock).toHaveBeenCalled());
    await vi.advanceTimersByTimeAsync(0);
    fetchMock.mockClear();

    el.libraryFilters.querySelector('[data-status="failed"]').dispatchEvent(new Event("click"));

    const names = [...el.libraryGrid.querySelectorAll(".video-filename")].map((n) => n.textContent);
    expect(names).toEqual(["broken.mkv"]);
    expect(fetchMock).not.toHaveBeenCalled();
  });

  it("pages forward by re-fetching with the next offset", async () => {
    const el = await loadApp();
    fetchMock.mockResolvedValueOnce(jsonResponse(JOBS));
    el.navLibrary.dispatchEvent(new Event("click"));
    await vi.waitFor(() => expect(fetchMock).toHaveBeenCalled());
    await vi.advanceTimersByTimeAsync(0);
    fetchMock.mockClear();
    fetchMock.mockResolvedValueOnce(jsonResponse([]));

    el.libraryNext.dispatchEvent(new Event("click"));
    await vi.waitFor(() => expect(fetchMock).toHaveBeenCalled());
    await vi.advanceTimersByTimeAsync(0);

    expect(fetchMock.mock.calls[0][0]).toBe("/api/jobs?limit=12&offset=12");
  });

  // --- deleting a video: an accidental upload, or general cleanup --------

  async function openLibraryWith(el, jobs) {
    fetchMock.mockResolvedValueOnce(jsonResponse(jobs));
    el.navLibrary.dispatchEvent(new Event("click"));
    await vi.waitFor(() => expect(fetchMock).toHaveBeenCalled());
    await vi.advanceTimersByTimeAsync(0);
    fetchMock.mockClear();
  }

  function deleteButtonFor(el, filename) {
    const card = [...el.libraryGrid.querySelectorAll(".video-card")].find((c) =>
      c.querySelector(".video-filename").textContent === filename,
    );
    return card.querySelector(".card-delete");
  }

  it("asks for confirmation before deleting, and does nothing if declined", async () => {
    const el = await loadApp();
    await openLibraryWith(el, JOBS);
    vi.spyOn(window, "confirm").mockReturnValue(false);

    deleteButtonFor(el, "holiday.mp4").dispatchEvent(new Event("click", { bubbles: true }));

    expect(window.confirm).toHaveBeenCalledOnce();
    expect(fetchMock).not.toHaveBeenCalled();
    const names = [...el.libraryGrid.querySelectorAll(".video-filename")].map((n) => n.textContent);
    expect(names).toContain("holiday.mp4");
  });

  it("deletes the video and removes its card once confirmed", async () => {
    const el = await loadApp();
    await openLibraryWith(el, JOBS);
    vi.spyOn(window, "confirm").mockReturnValue(true);
    fetchMock.mockResolvedValueOnce({ ok: true, status: 204, json: async () => ({}) });

    deleteButtonFor(el, "holiday.mp4").dispatchEvent(new Event("click", { bubbles: true }));
    await vi.waitFor(() => expect(fetchMock).toHaveBeenCalled());
    await vi.advanceTimersByTimeAsync(0);

    const [url, init] = fetchMock.mock.calls[0];
    expect(url).toBe("/api/jobs/1");
    expect(init.method).toBe("DELETE");
    const names = [...el.libraryGrid.querySelectorAll(".video-filename")].map((n) => n.textContent);
    expect(names).toEqual(["recap.mov", "broken.mkv"]);
  });

  it("does not also open the inline player when the delete button is clicked", async () => {
    const el = await loadApp();
    await openLibraryWith(el, JOBS);
    vi.spyOn(window, "confirm").mockReturnValue(false);

    const card = [...el.libraryGrid.querySelectorAll(".video-card")].find((c) =>
      c.querySelector(".video-filename").textContent === "holiday.mp4",
    );
    deleteButtonFor(el, "holiday.mp4").dispatchEvent(new Event("click", { bubbles: true }));

    expect(card.querySelector("video")).toBeNull();
  });

  it("shows an error and keeps the card if the delete fails", async () => {
    const el = await loadApp();
    await openLibraryWith(el, JOBS);
    vi.spyOn(window, "confirm").mockReturnValue(true);
    fetchMock.mockResolvedValueOnce(jsonResponse({ error: "not found" }, false, 404));

    deleteButtonFor(el, "holiday.mp4").dispatchEvent(new Event("click", { bubbles: true }));
    await vi.waitFor(() => expect(fetchMock).toHaveBeenCalled());
    await vi.advanceTimersByTimeAsync(0);

    expect(el.libraryStatus.textContent).toContain("not found");
    const names = [...el.libraryGrid.querySelectorAll(".video-filename")].map((n) => n.textContent);
    expect(names).toContain("holiday.mp4");
  });

  it("returns to the sign-in form if the session expired mid-delete", async () => {
    const el = await loadApp();
    await openLibraryWith(el, JOBS);
    vi.spyOn(window, "confirm").mockReturnValue(true);
    fetchMock.mockResolvedValueOnce(jsonResponse({ error: "not authenticated" }, false, 401));

    deleteButtonFor(el, "holiday.mp4").dispatchEvent(new Event("click", { bubbles: true }));
    await vi.waitFor(() => expect(fetchMock).toHaveBeenCalled());
    await vi.advanceTimersByTimeAsync(0);

    expect(el.auth.hidden).toBe(false);
    expect(el.authStatus.textContent).toContain("session expired");
  });

  it("shows a status the badge has no styling for, verbatim", async () => {
    const el = await loadApp();
    await openLibraryWith(el, [{ id: "9", filename: "odd.mp4", status: "cancelled" }]);

    expect(el.libraryGrid.querySelector(".badge").textContent).toContain("cancelled");
  });

  it("keeps the card and says why when a library delete is refused", async () => {
    const el = await loadApp();
    await openLibraryWith(el, JOBS);
    vi.spyOn(window, "confirm").mockReturnValue(true);
    fetchMock.mockResolvedValueOnce(jsonResponse({ error: "not found" }, false, 404));

    deleteButtonFor(el, "holiday.mp4").dispatchEvent(new Event("click", { bubbles: true }));
    await vi.waitFor(() => expect(fetchMock).toHaveBeenCalled());
    await vi.advanceTimersByTimeAsync(0);

    expect(el.libraryStatus.textContent).toContain("not found");
  });

  it("reports a network failure while deleting a library card", async () => {
    const el = await loadApp();
    await openLibraryWith(el, JOBS);
    vi.spyOn(window, "confirm").mockReturnValue(true);
    fetchMock.mockRejectedValueOnce(new Error("connection refused"));

    deleteButtonFor(el, "holiday.mp4").dispatchEvent(new Event("click", { bubbles: true }));
    await vi.waitFor(() => expect(fetchMock).toHaveBeenCalled());
    await vi.advanceTimersByTimeAsync(0);

    expect(el.libraryStatus.textContent).toContain("connection refused");
  });

  it("returns to the sign-in form when the library request says the session expired", async () => {
    const el = await loadApp();
    fetchMock.mockResolvedValueOnce(jsonResponse({ error: "not authenticated" }, false, 401));

    el.navLibrary.dispatchEvent(new Event("click"));
    await vi.waitFor(() => expect(fetchMock).toHaveBeenCalled());
    await vi.advanceTimersByTimeAsync(0);

    expect(el.auth.hidden).toBe(false);
    expect(el.authStatus.textContent).toContain("session expired");
  });

  it("shows an empty library rather than nothing when the list request fails", async () => {
    const el = await loadApp();
    fetchMock.mockResolvedValueOnce(jsonResponse({ error: "boom" }, false, 500));

    el.navLibrary.dispatchEvent(new Event("click"));
    await vi.waitFor(() => expect(fetchMock).toHaveBeenCalled());
    await vi.advanceTimersByTimeAsync(0);

    expect(el.libraryEmpty.hidden).toBe(false);
    expect(el.libraryGrid.hidden).toBe(true);
  });

  it("shows an empty library when the list request cannot be made at all", async () => {
    const el = await loadApp();
    fetchMock.mockRejectedValueOnce(new Error("connection refused"));

    el.navLibrary.dispatchEvent(new Event("click"));
    await vi.waitFor(() => expect(fetchMock).toHaveBeenCalled());
    await vi.advanceTimersByTimeAsync(0);

    expect(el.libraryEmpty.hidden).toBe(false);
  });

  it("pages back without going below the first page", async () => {
    const el = await loadApp();
    await openLibraryWith(el, JOBS);
    fetchMock.mockResolvedValueOnce(jsonResponse(JOBS));

    el.libraryPrev.dispatchEvent(new Event("click"));
    await vi.waitFor(() => expect(fetchMock).toHaveBeenCalled());
    await vi.advanceTimersByTimeAsync(0);

    expect(fetchMock.mock.calls[0][0]).toBe("/api/jobs?limit=12&offset=0");
  });
});

describe("admin", () => {
  const ROWS = [
    { id: "1", filename: "a.mp4", status: "done", output_url: "http://minio/a.mp4" },
    { id: "2", filename: "b.mp4", status: "queued" },
  ];

  it("fetches every job when an operator opens the tab", async () => {
    const el = await loadApp({ role: "operator" });
    fetchMock.mockResolvedValueOnce(jsonResponse(ROWS));

    el.navAdmin.dispatchEvent(new Event("click"));
    await vi.waitFor(() => expect(fetchMock).toHaveBeenCalled());
    await vi.advanceTimersByTimeAsync(0);

    expect(fetchMock.mock.calls[0][0]).toBe("/api/admin/jobs?limit=12&offset=0");
    expect(el.viewAdmin.hidden).toBe(false);
  });

  it("renders one row per job", async () => {
    const el = await loadApp({ role: "operator" });
    fetchMock.mockResolvedValueOnce(jsonResponse(ROWS));

    el.navAdmin.dispatchEvent(new Event("click"));
    await vi.waitFor(() => expect(fetchMock).toHaveBeenCalled());
    await vi.advanceTimersByTimeAsync(0);

    expect(el.adminTbody.querySelectorAll("tr").length).toBe(2);
  });

  it("falls back to the upload view if the API refuses with 403", async () => {
    const el = await loadApp({ role: "operator" });
    fetchMock.mockResolvedValueOnce(jsonResponse({ error: "operator role required" }, false, 403));

    el.navAdmin.dispatchEvent(new Event("click"));
    await vi.waitFor(() => expect(fetchMock).toHaveBeenCalled());
    await vi.advanceTimersByTimeAsync(0);

    expect(el.viewUpload.hidden).toBe(false);
    expect(el.viewAdmin.hidden).toBe(true);
  });

  // --- previewing without downloading, and deleting any user's upload ----

  async function openAdminWith(el, rows) {
    fetchMock.mockResolvedValueOnce(jsonResponse(rows));
    el.navAdmin.dispatchEvent(new Event("click"));
    await vi.waitFor(() => expect(fetchMock).toHaveBeenCalled());
    await vi.advanceTimersByTimeAsync(0);
    fetchMock.mockClear();
  }

  function rowFor(el, filename) {
    return [...el.adminTbody.querySelectorAll("tr")].find(
      (r) => r.firstElementChild.textContent === filename,
    );
  }

  it("plays a done job inline rather than linking to a downloadable URL", async () => {
    const el = await loadApp({ role: "operator" });
    await openAdminWith(el, ROWS);

    const row = rowFor(el, "a.mp4");
    expect(row.querySelector("a")).toBeNull();
    const watchButton = row.querySelector(".admin-watch");
    expect(watchButton.textContent).toBe("Watch");

    watchButton.dispatchEvent(new Event("click"));

    expect(fetchMock).not.toHaveBeenCalled();
    const video = row.querySelector("video");
    expect(video.getAttribute("src")).toBe("http://minio/a.mp4");
    expect(watchButton.textContent).toBe("Hide");
  });

  it("hides the player again on a second click", async () => {
    const el = await loadApp({ role: "operator" });
    await openAdminWith(el, ROWS);
    const row = rowFor(el, "a.mp4");
    const watchButton = row.querySelector(".admin-watch");

    watchButton.dispatchEvent(new Event("click"));
    watchButton.dispatchEvent(new Event("click"));

    expect(row.querySelector("video")).toBeNull();
    expect(watchButton.textContent).toBe("Watch");
  });

  it("shows no preview control for a job with no output yet", async () => {
    const el = await loadApp({ role: "operator" });
    await openAdminWith(el, ROWS);

    const row = rowFor(el, "b.mp4");

    expect(row.querySelector(".admin-watch")).toBeNull();
    expect(row.querySelector("a")).toBeNull();
  });

  it("asks for confirmation before deleting, and does nothing if declined", async () => {
    const el = await loadApp({ role: "operator" });
    await openAdminWith(el, ROWS);
    vi.spyOn(window, "confirm").mockReturnValue(false);

    rowFor(el, "a.mp4").querySelector(".card-delete").dispatchEvent(new Event("click"));

    expect(window.confirm).toHaveBeenCalledOnce();
    expect(fetchMock).not.toHaveBeenCalled();
    expect(rowFor(el, "a.mp4")).toBeTruthy();
  });

  it("deletes any user's job through the unscoped admin route", async () => {
    const el = await loadApp({ role: "operator" });
    await openAdminWith(el, ROWS);
    vi.spyOn(window, "confirm").mockReturnValue(true);
    fetchMock.mockResolvedValueOnce({ ok: true, status: 204, json: async () => ({}) });

    rowFor(el, "a.mp4").querySelector(".card-delete").dispatchEvent(new Event("click"));
    await vi.waitFor(() => expect(fetchMock).toHaveBeenCalled());
    await vi.advanceTimersByTimeAsync(0);

    const [url, init] = fetchMock.mock.calls[0];
    expect(url).toBe("/api/admin/jobs/1");
    expect(init.method).toBe("DELETE");
    expect(el.adminTbody.querySelectorAll("tr").length).toBe(1);
    expect(rowFor(el, "a.mp4")).toBeUndefined();
  });

  it("shows an error and keeps the row if the admin delete fails", async () => {
    const el = await loadApp({ role: "operator" });
    await openAdminWith(el, ROWS);
    vi.spyOn(window, "confirm").mockReturnValue(true);
    fetchMock.mockResolvedValueOnce(jsonResponse({ error: "not found" }, false, 404));

    rowFor(el, "a.mp4").querySelector(".card-delete").dispatchEvent(new Event("click"));
    await vi.waitFor(() => expect(fetchMock).toHaveBeenCalled());
    await vi.advanceTimersByTimeAsync(0);

    expect(el.adminStatus.textContent).toContain("not found");
    expect(rowFor(el, "a.mp4")).toBeTruthy();
  });

  it("filters the table without asking the API again", async () => {
    const el = await loadApp({ role: "operator" });
    await openAdminWith(el, ROWS);
    fetchMock.mockClear();

    el.adminFilters.querySelector('[data-status="queued"]').dispatchEvent(new Event("click"));

    expect(fetchMock).not.toHaveBeenCalled();
    expect(el.adminTbody.querySelectorAll("tr")).toHaveLength(1);
  });

  it("offers a preview for a job whose only source is the ladder", async () => {
    const el = await loadApp({ role: "operator" });
    await openAdminWith(el, [
      { id: "9", filename: "c.mp4", status: "done", hls_url: "/api/jobs/9/hls/master.m3u8" },
    ]);

    expect(rowFor(el, "c.mp4").querySelector(".admin-watch")).not.toBeNull();
  });

  it("returns to the sign-in form when the admin list says the session expired", async () => {
    const el = await loadApp({ role: "operator" });
    fetchMock.mockResolvedValueOnce(jsonResponse({ error: "not authenticated" }, false, 401));

    el.navAdmin.dispatchEvent(new Event("click"));
    await vi.waitFor(() => expect(fetchMock).toHaveBeenCalled());
    await vi.advanceTimersByTimeAsync(0);

    expect(el.auth.hidden).toBe(false);
    expect(el.authStatus.textContent).toContain("session expired");
  });

  it("shows an empty table rather than none when the admin list request fails", async () => {
    const el = await loadApp({ role: "operator" });
    fetchMock.mockResolvedValueOnce(jsonResponse({ error: "boom" }, false, 500));

    el.navAdmin.dispatchEvent(new Event("click"));
    await vi.waitFor(() => expect(fetchMock).toHaveBeenCalled());
    await vi.advanceTimersByTimeAsync(0);

    expect(el.adminEmpty.hidden).toBe(false);
  });

  it("shows an empty table when the admin list request cannot be made at all", async () => {
    const el = await loadApp({ role: "operator" });
    fetchMock.mockRejectedValueOnce(new Error("connection refused"));

    el.navAdmin.dispatchEvent(new Event("click"));
    await vi.waitFor(() => expect(fetchMock).toHaveBeenCalled());
    await vi.advanceTimersByTimeAsync(0);

    expect(el.adminEmpty.hidden).toBe(false);
  });

  it("returns to the sign-in form if the session expired mid-admin-delete", async () => {
    const el = await loadApp({ role: "operator" });
    await openAdminWith(el, ROWS);
    vi.spyOn(window, "confirm").mockReturnValue(true);
    fetchMock.mockResolvedValueOnce(jsonResponse({ error: "not authenticated" }, false, 401));

    rowFor(el, "a.mp4").querySelector(".card-delete").dispatchEvent(new Event("click"));
    await vi.waitFor(() => expect(fetchMock).toHaveBeenCalled());
    await vi.advanceTimersByTimeAsync(0);

    expect(el.auth.hidden).toBe(false);
    expect(el.authStatus.textContent).toContain("session expired");
  });

  it("leaves the admin view if a delete says the operator role is gone", async () => {
    const el = await loadApp({ role: "operator" });
    await openAdminWith(el, ROWS);
    vi.spyOn(window, "confirm").mockReturnValue(true);
    fetchMock.mockResolvedValueOnce(jsonResponse({ error: "operator role required" }, false, 403));

    rowFor(el, "a.mp4").querySelector(".card-delete").dispatchEvent(new Event("click"));
    await vi.waitFor(() => expect(fetchMock).toHaveBeenCalled());
    await vi.advanceTimersByTimeAsync(0);

    expect(el.viewAdmin.hidden).toBe(true);
    expect(el.viewUpload.hidden).toBe(false);
  });

  it("reports a network failure while deleting another user's job", async () => {
    const el = await loadApp({ role: "operator" });
    await openAdminWith(el, ROWS);
    vi.spyOn(window, "confirm").mockReturnValue(true);
    fetchMock.mockRejectedValueOnce(new Error("connection refused"));

    rowFor(el, "a.mp4").querySelector(".card-delete").dispatchEvent(new Event("click"));
    await vi.waitFor(() => expect(fetchMock).toHaveBeenCalled());
    await vi.advanceTimersByTimeAsync(0);

    expect(el.adminStatus.textContent).toContain("connection refused");
  });

  it("pages forward by re-fetching with the next offset", async () => {
    const el = await loadApp({ role: "operator" });
    await openAdminWith(el, ROWS);
    fetchMock.mockResolvedValueOnce(jsonResponse(ROWS));

    el.adminNext.dispatchEvent(new Event("click"));
    await vi.waitFor(() => expect(fetchMock).toHaveBeenCalled());
    await vi.advanceTimersByTimeAsync(0);

    expect(fetchMock.mock.calls[0][0]).toBe("/api/admin/jobs?limit=12&offset=12");
  });

  it("pages back without going below the first page of jobs", async () => {
    const el = await loadApp({ role: "operator" });
    await openAdminWith(el, ROWS);
    fetchMock.mockResolvedValueOnce(jsonResponse(ROWS));

    el.adminPrev.dispatchEvent(new Event("click"));
    await vi.waitFor(() => expect(fetchMock).toHaveBeenCalled());
    await vi.advanceTimersByTimeAsync(0);

    expect(fetchMock.mock.calls[0][0]).toBe("/api/admin/jobs?limit=12&offset=0");
  });
});

// ---------------------------------------------------------------------------
// The player. Plyr and hls.js arrive as separate vendored scripts that
// index.html loads, so these tests bring their own stand-ins. What is worth
// pinning down is not their internals but the app's half of the bargain:
// which source a job gets, where the level list comes from, and that nothing
// is left running once a card is closed or re-rendered.
// ---------------------------------------------------------------------------

const HLS_EVENTS = { MANIFEST_PARSED: "manifestParsed", ERROR: "error" };

/** Stand-in for the vendored Plyr: it only augments the element it was handed,
 *  so the fake's job is to record whether it was ever destroyed. */
class FakePlyr {
  constructor(element, config) {
    this.media = element;
    this.config = config;
    this.quality = undefined;
    this.destroyed = false;
    // What the real one does to the element it takes over.
    element.removeAttribute("controls");
    FakePlyr.created.push(this);
  }

  destroy() {
    this.destroyed = true;
  }
}
FakePlyr.created = [];

/** Stand-in for the vendored hls.js, with just enough surface for the app's
 *  half: the level list, the two events it listens for, and destroy(). */
class FakeHls {
  constructor() {
    this.levels = FakeHls.levels;
    this.handlers = {};
    this.attachedTo = null;
    this.loadedSource = null;
    this.currentLevel = -1;
    this.destroyed = false;
    FakeHls.instances.push(this);
  }

  static isSupported() {
    return true;
  }

  on(event, handler) {
    this.handlers[event] = [...(this.handlers[event] ?? []), handler];
  }

  /** Fire an hls.js event at the app's own listeners. */
  emit(event, data = {}) {
    for (const handler of this.handlers[event] ?? []) handler(event, data);
  }

  attachMedia(video) {
    this.attachedTo = video;
  }

  loadSource(url) {
    this.loadedSource = url;
  }

  destroy() {
    this.destroyed = true;
  }
}
FakeHls.instances = [];
FakeHls.Events = HLS_EVENTS;
FakeHls.levels = [{ height: 720 }, { height: 480 }, { height: 360 }];

describe("player", () => {
  const DONE = {
    id: "1",
    filename: "holiday.mp4",
    status: "done",
    output_url: "http://minio/holiday.mp4",
  };
  const ADAPTIVE = { ...DONE, hls_url: "/api/jobs/1/hls/master.m3u8" };

  beforeEach(() => {
    FakePlyr.created = [];
    FakeHls.instances = [];
  });

  afterEach(() => {
    // The libraries are globals the page loads; leaving them behind would make
    // every later test run with a player it did not ask for.
    delete window.Plyr;
    delete window.Hls;
  });

  /** The page as index.html delivers it: both libraries loaded. */
  async function loadAppWithLibraries(options) {
    const el = await loadApp(options);
    window.Plyr = FakePlyr;
    window.Hls = FakeHls;
    return el;
  }

  async function openLibrary(el, jobs) {
    fetchMock.mockResolvedValueOnce(jsonResponse(jobs));
    el.navLibrary.dispatchEvent(new Event("click"));
    await vi.waitFor(() => expect(fetchMock).toHaveBeenCalled());
    await vi.advanceTimersByTimeAsync(0);
  }

  function cardFor(el, filename) {
    return [...el.libraryGrid.querySelectorAll(".video-card")].find(
      (c) => c.querySelector(".video-filename").textContent === filename,
    );
  }

  function play(card) {
    card.querySelector(".video-thumb").dispatchEvent(new Event("click"));
  }

  function videoIn(card) {
    return card.querySelector(".player-host video");
  }

  it("plays a job's MP4 through Plyr when it has no ladder", async () => {
    const el = await loadAppWithLibraries();
    await openLibrary(el, [DONE]);

    play(cardFor(el, "holiday.mp4"));

    expect(FakePlyr.created).toHaveLength(1);
    expect(FakePlyr.created[0].media.getAttribute("src")).toBe(DONE.output_url);
    // No ladder, no level list -- and therefore no quality menu to offer.
    expect(FakePlyr.created[0].config.quality).toBeUndefined();
  });

  it("plays a job's ladder through hls.js rather than its MP4", async () => {
    const el = await loadAppWithLibraries();
    await openLibrary(el, [ADAPTIVE]);
    const card = cardFor(el, "holiday.mp4");

    play(card);

    const hls = FakeHls.instances[0];
    expect(hls.attachedTo).toBe(videoIn(card));
    expect(hls.loadedSource).toBe(ADAPTIVE.hls_url);
  });

  it("waits for the manifest, so the menu lists the ladder's own levels", async () => {
    const el = await loadAppWithLibraries();
    await openLibrary(el, [ADAPTIVE]);

    play(cardFor(el, "holiday.mp4"));

    // Plyr builds its quality menu once, from the list it is handed: creating
    // it before the levels are known would offer renditions that might not be
    // in the ladder at all, including an upscaled 720p a 480p source lacks.
    expect(FakePlyr.created).toHaveLength(0);

    FakeHls.instances[0].emit(HLS_EVENTS.MANIFEST_PARSED);

    expect(FakePlyr.created).toHaveLength(1);
    expect(FakePlyr.created[0].config.quality.forced).toBe(true);
    expect(FakePlyr.created[0].config.quality.options).toEqual([0, 720, 480, 360]);
    expect(FakePlyr.created[0].config.i18n.qualityLabel).toEqual({ 0: "Auto" });
    // The settings menu reads the player's current quality, and "undefined" is
    // what it says unless the app tells it where the ladder starts.
    expect(FakePlyr.created[0].quality).toBe(0);
  });

  it("switches the hls.js level when a quality is picked, and back to auto", async () => {
    const el = await loadAppWithLibraries();
    await openLibrary(el, [ADAPTIVE]);
    play(cardFor(el, "holiday.mp4"));
    const hls = FakeHls.instances[0];
    hls.emit(HLS_EVENTS.MANIFEST_PARSED);

    const { onChange } = FakePlyr.created[0].config.quality;
    onChange(480);
    expect(hls.currentLevel).toBe(1);

    onChange(0);
    expect(hls.currentLevel).toBe(-1);
  });

  it("destroys both libraries when the card is closed again", async () => {
    const el = await loadAppWithLibraries();
    await openLibrary(el, [ADAPTIVE]);
    const card = cardFor(el, "holiday.mp4");

    play(card);
    const hls = FakeHls.instances[0];
    hls.emit(HLS_EVENTS.MANIFEST_PARSED);
    play(card);

    expect(hls.destroyed).toBe(true);
    expect(FakePlyr.created[0].destroyed).toBe(true);
    expect(card.querySelector("video")).toBeNull();
    expect(card.querySelector(".player-host")).toBeNull();
  });

  it("tears the player down before a re-render throws its card away", async () => {
    const el = await loadAppWithLibraries();
    await openLibrary(el, [DONE]);
    play(cardFor(el, "holiday.mp4"));

    // Filtering re-renders the whole grid, cards and players included.
    el.libraryFilters.querySelector('[data-status="failed"]').dispatchEvent(new Event("click"));

    expect(FakePlyr.created[0].destroyed).toBe(true);
    expect(el.libraryGrid.querySelector(".player-host")).toBeNull();
  });

  it("does not collapse the card when the player itself is clicked", async () => {
    const el = await loadAppWithLibraries();
    await openLibrary(el, [DONE]);
    const card = cardFor(el, "holiday.mp4");
    play(card);
    const host = card.querySelector(".player-host");

    // Every control in the player sits inside the element whose click opened
    // it, so a press of play would otherwise close the card it is playing in.
    host.dispatchEvent(new Event("click", { bubbles: true }));

    expect(card.querySelector(".player-host")).toBe(host);
  });

  it("leaves a plain playable video behind when Plyr never loads", async () => {
    const el = await loadApp();
    await openLibrary(el, [ADAPTIVE]);

    play(cardFor(el, "holiday.mp4"));

    const video = videoIn(cardFor(el, "holiday.mp4"));
    expect(video.getAttribute("src")).toBe(DONE.output_url);
    expect(video.controls).toBe(true);
  });

  it("falls back to the MP4 when hls.js is missing but a ladder exists", async () => {
    const el = await loadApp();
    window.Plyr = FakePlyr;
    await openLibrary(el, [ADAPTIVE]);

    play(cardFor(el, "holiday.mp4"));

    expect(videoIn(cardFor(el, "holiday.mp4")).getAttribute("src")).toBe(DONE.output_url);
    expect(FakePlyr.created[0].config.quality).toBeUndefined();
  });

  it("drops back to the MP4 when the ladder fails fatally", async () => {
    const el = await loadAppWithLibraries();
    await openLibrary(el, [ADAPTIVE]);
    play(cardFor(el, "holiday.mp4"));
    const hls = FakeHls.instances[0];
    hls.emit(HLS_EVENTS.MANIFEST_PARSED);

    hls.emit(HLS_EVENTS.ERROR, { fatal: true });
    await vi.advanceTimersByTimeAsync(0);

    expect(videoIn(cardFor(el, "holiday.mp4")).getAttribute("src")).toBe(DONE.output_url);
    // Rebuilt rather than repointed: the first player's menu was built around
    // a ladder that turned out not to play.
    expect(FakePlyr.created[1].config.quality).toBeUndefined();
    expect(FakeHls.instances).toHaveLength(1);
    expect(hls.destroyed).toBe(true);
  });

  it("does not build a player into a card collapsed while the manifest loaded", async () => {
    const el = await loadAppWithLibraries();
    await openLibrary(el, [ADAPTIVE]);
    const card = cardFor(el, "holiday.mp4");

    play(card);
    const hls = FakeHls.instances[0];
    play(card);

    hls.emit(HLS_EVENTS.MANIFEST_PARSED);

    expect(FakePlyr.created).toHaveLength(0);
    expect(hls.destroyed).toBe(true);
  });

  it("plays the upload card's finished job through Plyr too", async () => {
    const el = await loadAppWithLibraries();
    fetchMock
      .mockResolvedValueOnce(jsonResponse({ job_id: "job-1" }))
      .mockResolvedValue(jsonResponse({ status: "done", output_url: DONE.output_url }));

    await uploadFile(el);

    expect(FakePlyr.created).toHaveLength(1);
    expect(FakePlyr.created[0].media.getAttribute("src")).toBe(DONE.output_url);
  });

  it("swaps the upload card's MP4 for the ladder when the ladder lands", async () => {
    const el = await loadAppWithLibraries();
    mockDirectUpload();
    fetchMock
      .mockResolvedValueOnce(
        jsonResponse({
          id: "job-1",
          status: "done",
          output_url: DONE.output_url,
          hls_status: "pending",
        }),
      )
      .mockResolvedValue(
        jsonResponse({
          id: "job-1",
          status: "done",
          output_url: DONE.output_url,
          hls_url: ADAPTIVE.hls_url,
          hls_status: "ready",
        }),
      );

    await uploadFile(el);

    expect(FakePlyr.created).toHaveLength(1);
    expect(FakePlyr.created[0].media.getAttribute("src")).toBe(DONE.output_url);

    await vi.advanceTimersByTimeAsync(2000);

    // Rebuilt once, onto the adaptive source -- not once per poll. The MP4's
    // player is torn down rather than left running against an element nobody
    // can see.
    expect(FakeHls.instances).toHaveLength(1);
    expect(FakeHls.instances[0].loadedSource).toBe(ADAPTIVE.hls_url);
    expect(FakePlyr.created[0].destroyed).toBe(true);

    // Plyr still waits for the manifest, exactly as it does from a Library
    // card, so the quality menu is built from the ladder's real rungs.
    FakeHls.instances[0].emit(HLS_EVENTS.MANIFEST_PARSED);
    expect(FakePlyr.created).toHaveLength(2);
    expect(el.status.textContent).toBe("Processing complete.");
  });

  it("gives an operator's admin preview the same player", async () => {
    const el = await loadAppWithLibraries({ role: "operator" });
    fetchMock.mockResolvedValueOnce(jsonResponse([DONE]));
    el.navAdmin.dispatchEvent(new Event("click"));
    await vi.waitFor(() => expect(fetchMock).toHaveBeenCalled());
    await vi.advanceTimersByTimeAsync(0);

    el.adminTbody.querySelector(".admin-watch").dispatchEvent(new Event("click"));

    expect(FakePlyr.created).toHaveLength(1);
  });
});

// ---------------------------------------------------------------------------
// The editing panel. It is one view over the contract in sprint3-plan.md §1,
// so these tests are about two things: what it sends, and what it refuses to
// send. Crop and clip belong to other tracks, so the seam with them is a
// stand-in here -- EDITOR-COMPONENTS.md is the contract they implement.
// ---------------------------------------------------------------------------

describe("editor", () => {
  const SOURCE = {
    id: "1",
    filename: "holiday.mp4",
    status: "done",
    output_url: "http://minio/holiday.mp4",
  };

  beforeEach(() => {
    FakePlyr.created = [];
    window.Plyr = FakePlyr;
  });

  afterEach(() => {
    delete window.Plyr;
    delete window.mountCropBox;
    delete window.mountClipScrubber;
  });

  async function openLibraryWith(el, jobs) {
    fetchMock.mockResolvedValueOnce(jsonResponse(jobs));
    el.navLibrary.dispatchEvent(new Event("click"));
    await vi.waitFor(() => expect(fetchMock).toHaveBeenCalled());
    await vi.advanceTimersByTimeAsync(0);
    fetchMock.mockClear();
  }

  /** Open the editor from the first Library card that offers it. */
  async function openEditor(el, jobs = [SOURCE]) {
    await openLibraryWith(el, jobs);
    const button = el.libraryGrid.querySelector(".card-edit");
    expect(button, "no card offered Edit").toBeTruthy();
    button.dispatchEvent(new Event("click"));
    return el;
  }

  /** A stand-in for one of B's or C's components: it records the element it
   *  was handed, and reports whatever the test tells it to. */
  function fakeComponent() {
    return {
      calls: [],
      cleanup: vi.fn(),
      mount(video, onChange) {
        this.calls.push({ video, onChange });
        return this.cleanup;
      },
      report(selection) {
        this.calls.at(-1).onChange(selection);
      },
    };
  }

  function submit(el) {
    el.editForm.dispatchEvent(new Event("submit", { cancelable: true }));
  }

  /** Answer the three requests an edit makes: the POST, the polls, and the
   *  library refresh the result triggers. `documents` is what each poll
   *  returns, with the last one repeating. */
  function mockEditFlow(documents) {
    let polls = 0;
    fetchMock.mockImplementation(async (url) => {
      if (url === "/api/jobs/1/edit") return jsonResponse({ job_id: "edit-1" }, true, 202);
      if (url === "/api/jobs/edit-1") {
        const document = documents[Math.min(polls, documents.length - 1)];
        polls += 1;
        return jsonResponse(document);
      }
      if (url.startsWith("/api/jobs?")) return jsonResponse([]);
      return jsonResponse({ error: `unexpected request to ${url}` }, false, 500);
    });
  }

  const EDITED = {
    id: "edit-1",
    filename: "holiday-crop.mp4",
    status: "done",
    output_url: "http://minio/holiday-crop.mp4",
  };

  /** The operations the panel actually sent, in a stable order -- the array's
   *  order means nothing to the worker, so nothing here should depend on it. */
  function sentOperations() {
    const call = fetchMock.mock.calls.find(([url]) => url.endsWith("/edit"));
    expect(call, "no edit request was sent").toBeTruthy();
    const [url, init] = call;
    expect(url).toBe("/api/jobs/1/edit");
    expect(init.method).toBe("POST");
    const { operations } = JSON.parse(init.body);
    return [...operations].sort((a, b) => a.operation.localeCompare(b.operation));
  }

  it("opens from a finished library entry, on that job", async () => {
    const el = await loadApp();
    await openEditor(el);

    expect(el.viewEdit.hidden).toBe(false);
    expect(el.editSource.textContent).toBe("holiday.mp4");
    expect(el.editPlayer.querySelector("video").getAttribute("src")).toBe(SOURCE.output_url);
    expect(FakePlyr.created).toHaveLength(1);
  });

  it("offers no Edit for a job that has not finished", async () => {
    const el = await loadApp();
    await openLibraryWith(el, [{ id: "2", filename: "recap.mov", status: "processing" }]);

    expect(el.libraryGrid.querySelector(".card-edit")).toBeNull();
  });

  it("opens from the upload card once its job is done, and not before", async () => {
    const el = await loadApp();
    mockDirectUpload();
    fetchMock.mockResolvedValueOnce(jsonResponse({ id: "job-1", status: "processing" }));
    fetchMock.mockResolvedValue(
      jsonResponse({ ...SOURCE, id: "job-1", filename: "uploaded.mp4" }),
    );

    await uploadFile(el);
    expect(el.jobEdit.hidden).toBe(true);

    await vi.advanceTimersByTimeAsync(4000);

    expect(el.jobEdit.hidden).toBe(false);
    el.jobEdit.dispatchEvent(new Event("click"));
    expect(el.viewEdit.hidden).toBe(false);
    expect(el.editSource.textContent).toBe("uploaded.mp4");
  });

  it("disables the sections whose components have not landed", async () => {
    const el = await loadApp();
    await openEditor(el);

    expect(el.editCrop.disabled).toBe(true);
    expect(el.editClip.disabled).toBe(true);
    // Visitors see this, so it says what they can expect -- not which of
    // the team's tracks owns the missing component.
    expect(el.editCropMount.textContent).toContain("next update");
    expect(el.editClipMount.textContent).toContain("next update");
    expect(el.editCropMount.textContent).not.toMatch(/Track [A-E]/);
    expect(el.editClipMount.textContent).not.toMatch(/Track [A-E]/);
    // The three the panel owns are always usable.
    expect(el.editDownscale.disabled).toBe(false);
    expect(el.editUpscale.disabled).toBe(false);
    expect(el.editConvert.disabled).toBe(false);
  });

  it("mounts crop and clip against the video, and sends what they report", async () => {
    const crop = fakeComponent();
    const clip = fakeComponent();
    window.mountCropBox = crop.mount.bind(crop);
    window.mountClipScrubber = clip.mount.bind(clip);
    const el = await loadApp();
    await openEditor(el);

    const video = el.editPlayer.querySelector("video");
    expect(crop.calls[0].video).toBe(video);
    expect(clip.calls[0].video).toBe(video);
    expect(el.editCrop.disabled).toBe(false);

    el.editCrop.checked = true;
    el.editCrop.dispatchEvent(new Event("change"));
    el.editClip.checked = true;
    el.editClip.dispatchEvent(new Event("change"));
    // Ticked but with nothing selected yet: there is no rectangle or range to
    // send, and a request without them is a 422.
    expect(el.editProcess.disabled).toBe(true);

    crop.report({ x: 0, y: 140, w: 1080, h: 1080 });
    clip.report({ start: 5, end: 12.5 });
    expect(el.editProcess.disabled).toBe(false);

    submit(el);

    expect(sentOperations()).toEqual([
      { operation: "clip", params: { start: 5, end: 12.5 } },
      { operation: "crop", params: { x: 0, y: 140, w: 1080, h: 1080 } },
    ]);
  });

  it("builds the scale dropdowns from the video's own edit_options", async () => {
    const el = await loadApp();
    await openEditor(el, [
      {
        ...SOURCE,
        height: 720,
        edit_options: { downscale: [480, 360, 240], upscale: [1080, 1440, 2160] },
      },
    ]);

    expect([...el.editDownscaleHeight.options].map((o) => o.value)).toEqual(["480", "360", "240"]);
    expect([...el.editUpscaleHeight.options].map((o) => o.value)).toEqual(["1080", "1440", "2160"]);
    expect(el.editDownscaleSection.hidden).toBe(false);
    expect(el.editUpscaleSection.hidden).toBe(false);

    el.editUpscale.checked = true;
    el.editUpscale.dispatchEvent(new Event("change"));
    submit(el);

    // The dropdown the API filled is the height that goes on the wire.
    expect(sentOperations()).toEqual([{ operation: "upscale", params: { height: 1080 } }]);
  });

  it("hides the operation this video has no use for", async () => {
    const el = await loadApp();
    // A 2160p source: there is nothing above it to upscale to, so the API
    // sends an empty list and the section goes away rather than sitting there
    // permanently disabled.
    await openEditor(el, [
      {
        ...SOURCE,
        height: 2160,
        edit_options: { downscale: [1440, 1080, 720, 480, 360, 240], upscale: [] },
      },
    ]);

    expect(el.editUpscaleSection.hidden).toBe(true);
    expect(el.editUpscale.disabled).toBe(true);
    expect(el.editDownscaleSection.hidden).toBe(false);
  });

  it("keeps its own lists for a job the API has not measured", async () => {
    const el = await loadApp();
    // No stored dimensions means no edit_options, and the fixed lists -- with
    // the worker doing the checking -- are what sprint 3 shipped.
    await openEditor(el, [{ ...SOURCE }]);

    expect([...el.editDownscaleHeight.options].map((o) => o.value)).toEqual(["480", "360", "240"]);
    expect([...el.editUpscaleHeight.options].map((o) => o.value)).toEqual(["1080", "1440"]);
    expect(el.editDownscaleSection.hidden).toBe(false);
    expect(el.editUpscaleSection.hidden).toBe(false);
  });

  it("gives the fixed lists back after a job that replaced them", async () => {
    const el = await loadApp();
    await openEditor(el, [
      { ...SOURCE, height: 720, edit_options: { downscale: [480], upscale: [1080] } },
    ]);
    expect([...el.editDownscaleHeight.options].map((o) => o.value)).toEqual(["480"]);

    await openEditor(el, [{ ...SOURCE, id: "2" }]);

    expect([...el.editDownscaleHeight.options].map((o) => o.value)).toEqual(["480", "360", "240"]);
    expect([...el.editUpscaleHeight.options].map((o) => o.value)).toEqual(["1080", "1440"]);
  });

  it("cannot be made to send downscale and upscale together", async () => {
    const el = await loadApp();
    await openEditor(el);

    el.editDownscale.checked = true;
    el.editDownscale.dispatchEvent(new Event("change"));
    expect(el.editUpscale.disabled).toBe(true);

    // Even if the checkbox is forced, ticking it closes the other one -- the
    // worker answers this combination with a 422, so the panel must not be
    // able to build it.
    el.editUpscale.checked = true;
    el.editUpscale.dispatchEvent(new Event("change"));
    expect(el.editDownscale.checked).toBe(false);

    submit(el);

    expect(sentOperations()).toEqual([
      { operation: "upscale", params: { height: 1080 } },
    ]);
  });

  it("sends the operations the panel owns with the chosen values", async () => {
    const el = await loadApp();
    await openEditor(el);

    el.editDownscale.checked = true;
    el.editDownscale.dispatchEvent(new Event("change"));
    el.editDownscaleHeight.value = "360";
    el.editConvert.checked = true;
    el.editConvert.dispatchEvent(new Event("change"));
    el.editConvertFormat.value = "mp3";

    submit(el);

    expect(sentOperations()).toEqual([
      { operation: "convert", params: { format: "mp3" } },
      { operation: "downscale", params: { height: 360 } },
    ]);
  });

  it("says what still has to be done before Process will send anything", async () => {
    const el = await loadApp();
    await openEditor(el);

    expect(el.editProcess.disabled).toBe(true);
    expect(el.editHint.textContent).toContain("Check an operation");

    el.editConvert.checked = true;
    el.editConvert.dispatchEvent(new Event("change"));

    expect(el.editProcess.disabled).toBe(false);
    expect(el.editHint.textContent).toBe("1 operation in one pass.");
  });

  it("follows the new job and offers the result", async () => {
    const el = await loadApp();
    await openEditor(el);
    mockEditFlow([{ id: "edit-1", status: "processing" }, EDITED]);

    el.editConvert.checked = true;
    el.editConvert.dispatchEvent(new Event("change"));
    submit(el);
    await vi.waitFor(() => expect(fetchMock).toHaveBeenCalledTimes(1));

    await vi.advanceTimersByTimeAsync(4000);

    expect(el.editResult.hidden).toBe(false);
    expect(el.editResultName.textContent).toBe(EDITED.filename);
    expect(el.editResultDownload.getAttribute("href")).toBe(EDITED.output_url);
    expect(el.editResultDownload.getAttribute("download")).toBe(EDITED.filename);
    expect(el.editResultPlayer.querySelector("video")).not.toBeNull();
    expect(el.editProgress.hidden).toBe(true);
  });

  it("warns that a converted result may not play inline", async () => {
    const el = await loadApp();
    await openEditor(el);
    mockEditFlow([
      { id: "edit-1", filename: "holiday.mkv", status: "done", output_url: "http://minio/holiday.mkv" },
    ]);

    el.editConvert.checked = true;
    el.editConvert.dispatchEvent(new Event("change"));
    submit(el);
    await vi.advanceTimersByTimeAsync(0);

    expect(el.editResultNote.hidden).toBe(false);
    expect(el.editResultNote.textContent).toContain("Download");
  });

  it("passes the API's own message back when the edit is refused", async () => {
    const el = await loadApp();
    await openEditor(el);
    fetchMock.mockResolvedValueOnce(
      jsonResponse({ detail: [{ msg: "downscale and upscale cannot be combined" }] }, false, 422),
    );

    el.editConvert.checked = true;
    el.editConvert.dispatchEvent(new Event("change"));
    submit(el);
    await vi.waitFor(() => expect(fetchMock).toHaveBeenCalled());
    await vi.advanceTimersByTimeAsync(0);

    expect(el.editStatus.textContent).toContain("downscale and upscale cannot be combined");
    expect(el.editStatus.textContent).toContain("422");
  });

  it("says which status it got when the endpoint is not there yet", async () => {
    const el = await loadApp();
    await openEditor(el);
    fetchMock.mockResolvedValueOnce(jsonResponse({ detail: "Not Found" }, false, 404));

    el.editConvert.checked = true;
    el.editConvert.dispatchEvent(new Event("change"));
    submit(el);
    await vi.waitFor(() => expect(fetchMock).toHaveBeenCalled());
    await vi.advanceTimersByTimeAsync(0);

    expect(el.editStatus.textContent).toBe("Could not start the edit: Not Found (HTTP 404)");
    // The panel is still usable, so a second attempt is not blocked.
    expect(el.editProcess.disabled).toBe(false);
  });

  it("returns to the sign-in form if the session expired mid-edit", async () => {
    const el = await loadApp();
    await openEditor(el);
    fetchMock.mockResolvedValueOnce(jsonResponse({ error: "not authenticated" }, false, 401));

    el.editConvert.checked = true;
    el.editConvert.dispatchEvent(new Event("change"));
    submit(el);
    await vi.waitFor(() => expect(fetchMock).toHaveBeenCalled());
    await vi.advanceTimersByTimeAsync(0);

    expect(el.auth.hidden).toBe(false);
    expect(el.authStatus.textContent).toContain("session expired");
  });

  it("stops following the edit, and tears the session down, on the way out", async () => {
    const crop = fakeComponent();
    window.mountCropBox = crop.mount.bind(crop);
    const el = await loadApp();
    await openEditor(el);
    mockEditFlow([{ id: "edit-1", status: "processing" }]);

    el.editConvert.checked = true;
    el.editConvert.dispatchEvent(new Event("change"));
    submit(el);
    await vi.advanceTimersByTimeAsync(4000);
    // Only the polls: going back also refetches the library, which is a
    // different request and not the thing being asserted here.
    const editsPolled = () =>
      fetchMock.mock.calls.filter(([url]) => url === "/api/jobs/edit-1").length;
    const pollsWhileOpen = editsPolled();
    expect(pollsWhileOpen).toBeGreaterThan(1);

    el.editBack.dispatchEvent(new Event("click"));
    await vi.advanceTimersByTimeAsync(20000);

    expect(editsPolled()).toBe(pollsWhileOpen);
    expect(crop.cleanup).toHaveBeenCalled();
    expect(el.editPlayer.querySelector("video")).toBeNull();
    expect(FakePlyr.created[0].destroyed).toBe(true);
    expect(el.viewLibrary.hidden).toBe(false);
  });
});
