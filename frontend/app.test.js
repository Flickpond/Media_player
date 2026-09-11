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
    status: document.getElementById("status"),
    activeJob: document.getElementById("active-job"),
    jobFilename: document.getElementById("job-filename"),
    jobBadge: document.getElementById("job-badge"),
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
    expect(el.player.hasAttribute("src")).toBe(false);
    expect(el.jobBadge.textContent).toBe("Queued");
  });

  it("rejects an unsupported type before ever calling the API", async () => {
    const el = await loadApp();

    chooseFile(el.fileInput, fileNamed("notes.pdf", "application/pdf"));
    el.fileInput.dispatchEvent(new Event("change"));

    expect(el.status.textContent).toContain("not a video format");
    expect(fetchMock).not.toHaveBeenCalled();
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

    expect(el.player.getAttribute("src")).toBe("http://minio/out.mp4");
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
    expect(el.player.getAttribute("src")).toBe("http://minio/late.mp4");
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
});
