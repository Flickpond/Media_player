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
async function loadApp({ signedIn = true, role = "user" } = {}) {
  document.body.innerHTML = BODY;
  vi.resetModules();
  fetchMock.mockResolvedValueOnce(
    signedIn
      ? jsonResponse({ id: "u-1", email: "maya@example.test", role })
      : jsonResponse({ error: "not authenticated" }, false, 401),
  );
  await import("./app.js");
  await vi.waitFor(() => expect(fetchMock).toHaveBeenCalledWith("/api/auth/me"));
  await vi.advanceTimersByTimeAsync(0);
  fetchMock.mockClear();
  return {
    dropzone: document.getElementById("dropzone"),
    fileInput: document.getElementById("file-input"),
    browseButton: document.getElementById("browse-button"),
    status: document.getElementById("status"),
    activeJob: document.getElementById("active-job"),
    jobFilename: document.getElementById("job-filename"),
    jobBadge: document.getElementById("job-badge"),
    jobDelete: document.getElementById("job-delete"),
    jobProgress: document.getElementById("job-progress"),
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
});

describe("upload", () => {
  it("posts the chosen file to /upload as multipart", async () => {
    const el = await loadApp();
    fetchMock.mockResolvedValue(jsonResponse({ job_id: "job-1", status: "queued" }));

    await uploadFile(el);

    const [url, init] = fetchMock.mock.calls[0];
    expect(url).toBe("/api/upload");
    expect(init.method).toBe("POST");
    expect(init.body.get("file")).toBeInstanceOf(File);
  });

  it("polls the job id the API returned", async () => {
    const el = await loadApp();
    fetchMock
      .mockResolvedValueOnce(jsonResponse({ job_id: "abc-123" }))
      .mockResolvedValueOnce(jsonResponse({ status: "queued" }));

    await uploadFile(el);

    expect(fetchMock.mock.calls[1][0]).toBe("/api/jobs/abc-123");
  });

  it("shows the filename and a queued badge as soon as the job is accepted", async () => {
    const el = await loadApp();
    fetchMock.mockResolvedValue(jsonResponse({ job_id: "job-1", status: "queued" }));

    await uploadFile(el, fileNamed("holiday.mp4"));

    expect(el.activeJob.hidden).toBe(false);
    expect(el.jobFilename.textContent).toBe("holiday.mp4");
    expect(el.jobBadge.textContent).toBe("Queued");
  });

  it("ignores a second file dropped while the first upload is still in flight", async () => {
    const el = await loadApp();
    let release;
    fetchMock.mockReturnValueOnce(
      new Promise((resolve) => {
        release = resolve;
      }),
    );

    chooseFile(el.fileInput, fileNamed("first.mp4"));
    el.fileInput.dispatchEvent(new Event("change"));
    await vi.waitFor(() => expect(fetchMock).toHaveBeenCalledTimes(1));
    expect(el.dropzone.classList.contains("busy")).toBe(true);

    // A second selection while the POST is still pending must not double-post.
    chooseFile(el.fileInput, fileNamed("second.mp4"));
    el.fileInput.dispatchEvent(new Event("change"));
    expect(fetchMock).toHaveBeenCalledTimes(1);

    release(jsonResponse({ job_id: "job-1" }));
    await vi.waitFor(() => expect(el.dropzone.classList.contains("busy")).toBe(false));
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
    fetchMock
      .mockResolvedValueOnce(jsonResponse({ job_id: "job-1" }))
      .mockResolvedValueOnce(jsonResponse({ status: "done", output_url: "http://minio/a.mp4" }));
    await uploadFile(el);
    expect(el.player.hidden).toBe(false);

    fetchMock.mockResolvedValue(jsonResponse({ job_id: "job-2", status: "queued" }));
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
    fetchMock
      .mockResolvedValueOnce(jsonResponse({ job_id: "job-1" }))
      .mockResolvedValue(jsonResponse({ status: "processing" }));
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

  it("refuses a file over the size limit without asking the API", async () => {
    const el = await loadApp();
    const huge = fileNamed("huge.mp4");
    Object.defineProperty(huge, "size", { value: 200 * 1024 * 1024 });

    chooseFile(el.fileInput, huge);
    el.fileInput.dispatchEvent(new Event("change"));

    expect(el.status.textContent).toContain("100MB");
    expect(fetchMock).not.toHaveBeenCalled();
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

    expect(fetchMock.mock.calls[0][0]).toBe("/api/upload");
  });
});

describe("polling", () => {
  /** Upload, then have every poll return the given job document. */
  async function pollReturning(job) {
    const el = await loadApp();
    fetchMock
      .mockResolvedValueOnce(jsonResponse({ job_id: "job-1" }))
      .mockResolvedValue(jsonResponse(job));
    await uploadFile(el);
    return el;
  }

  it("shows the player and stops polling once the job is done", async () => {
    const el = await pollReturning({ status: "done", output_url: "http://minio/out.mp4" });

    expect(el.player.querySelector("video").getAttribute("src")).toBe("http://minio/out.mp4");
    expect(el.player.hidden).toBe(false);
    expect(el.jobBadge.textContent).toBe("Done");
    await vi.advanceTimersByTimeAsync(10000);
    expect(fetchMock).toHaveBeenCalledTimes(2);
  });

  it("shows the error, offers a retry, and stops polling once the job has failed", async () => {
    const el = await pollReturning({ status: "failed", error: "source missing" });

    expect(el.status.textContent).toContain("source missing");
    expect(el.player.hidden).toBe(true);
    expect(el.jobActions.hidden).toBe(false);
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
    await vi.advanceTimersByTimeAsync(10000);
    // 1 upload + 1 immediate poll + 5 more at 2s intervals over the next 10s.
    expect(fetchMock).toHaveBeenCalledTimes(7);
  });

  it("shows the progress sweep only while the job is processing", async () => {
    const el = await pollReturning({ status: "processing" });

    expect(el.jobProgress.hidden).toBe(false);
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
