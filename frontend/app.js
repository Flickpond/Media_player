// Flickpond frontend entry point: sign in -> upload -> poll -> play, plus a
// library of past uploads and an operator view over everyone's jobs.
//
// Backend endpoints it depends on (see docs/contract.md):
//   POST /auth/register  -> 201 { id, email, role }, sets the session cookie
//   POST /auth/login     -> 200 { id, email, role }, sets the session cookie
//   POST /auth/logout    -> 204, clears it
//   GET  /auth/me        -> 200 { id, email, role } | 401
//   POST /upload         -> 202 { job_id }, sets no cookie
//   POST /uploads                    -> 201 { upload_id, part_size, part_count }
//   POST /uploads/{id}/parts         -> 200 { urls: { "1": "...", ... } }
//   GET  /uploads/{id}               -> 200 { parts_done, state, part_size, part_count, job_id? }
//   POST /uploads/{id}/complete      -> 202 { job_id }
//   DELETE /uploads/{id}             -> 204
//   GET  /limits         -> 200 { max_duration_seconds, max_edit_height }
//   GET  /jobs/{id}      -> { id, filename, status, output_url?, hls_url?,
//                             hls_status, thumbnail_url?, error? }
//   GET  /jobs?limit&offset       -> [ ...job shape... ]   (caller's own jobs)
//   GET  /admin/jobs?limit&offset -> [ ...job shape... ]   (operator only)
//
// `hls_url` is sprint 3's adaptive ladder: present when the worker built one,
// absent on every job uploaded before it and on any job whose ladder failed.
// The player falls back to `output_url` -- the MP4 -- whenever it is missing.
//
// `thumbnail_url` is sprint 4's poster frame (Track C), signed the same way
// `output_url` is. It is absent until the frame is extracted and on every job
// from before sprint 4, so the Library draws its gradient placeholder either
// way and swaps in the frame when the API hands one back.
//
// The `/uploads` routes are sprint 4's direct multipart upload (Track B, see
// docs/b-direct-uploads.md): the browser asks for a presigned URL per 16 MiB
// part and PUTs the bytes straight to storage, so the API never sees the
// video. `POST /upload` still exists and is still used as the fallback.
// Sprint 5 puts three parts in the air at once and gives the transfer a Cancel
// button (#67, #68): the part in flight is aborted, and the session is deleted
// rather than left behind for its 24 hour expiry.
//
// The session cookie is HttpOnly and sent automatically, so nothing here reads
// or attaches a token -- which is the point: a script cannot steal what it
// cannot read.
// nginx serves this page and proxies /api/ to the API on the internal network,
// so a relative base is correct wherever the site is hosted. It must not depend
// on the public port: a check like `location.port === "3000"` is false on 80 and
// 443, which is every real deployment, and the fallback would then point the
// browser at the *visitor's* own machine.
const API = "/api";

// Mirrors app/api/uploads.py's ALLOWED_CONTENT_TYPES. This check is a courtesy
// only -- it saves an obviously-wrong file a round trip -- never the source of
// truth: the server re-checks by content, not by what the browser claims a
// file is (T-01, T-02), and a mislabeled file still has to pass that.
const ALLOWED_CONTENT_TYPES = new Set([
  "video/mp4",
  "video/mpeg",
  "video/quicktime",
  "video/webm",
  "video/x-matroska",
  "video/x-msvideo",
]);
// Mirrors app/api/uploads.py's MAX_FILE_SIZE. Same caveat: advisory only.
const MAX_FILE_SIZE_BYTES = 100 * 1024 * 1024;

const PAGE_SIZE = 12;

const el = {
  auth: document.getElementById("auth"),
  authHeading: document.getElementById("auth-heading"),
  authSubtext: document.getElementById("auth-subtext"),
  authForm: document.getElementById("auth-form"),
  authEmail: document.getElementById("auth-email"),
  authPassword: document.getElementById("auth-password"),
  authPasswordHint: document.getElementById("auth-password-hint"),
  authSubmit: document.getElementById("auth-submit"),
  authToggle: document.getElementById("auth-toggle"),
  authToggleText: document.getElementById("auth-toggle-text"),
  authStatus: document.getElementById("auth-status"),

  app: document.getElementById("app"),
  who: document.getElementById("who"),
  logout: document.getElementById("logout"),
  operatorTag: document.getElementById("operator-tag"),
  navUpload: document.getElementById("nav-upload"),
  navLibrary: document.getElementById("nav-library"),
  navAdmin: document.getElementById("nav-admin"),

  viewUpload: document.getElementById("view-upload"),
  dropzone: document.getElementById("dropzone"),
  dropzoneHint: document.getElementById("dropzone-hint"),
  keep60fps: document.getElementById("keep-60fps"),
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
  jobEdit: document.getElementById("job-edit"),
  jobDelete: document.getElementById("job-delete"),
  jobProgress: document.getElementById("job-progress"),
  uploadProgress: document.getElementById("upload-progress"),
  uploadProgressTrack: document.getElementById("upload-progress-track"),
  uploadProgressFill: document.getElementById("upload-progress-fill"),
  uploadProgressPercent: document.getElementById("upload-progress-percent"),
  uploadProgressRate: document.getElementById("upload-progress-rate"),
  uploadProgressEta: document.getElementById("upload-progress-eta"),
  uploadCancel: document.getElementById("upload-cancel"),
  player: document.getElementById("player"),
  jobActions: document.getElementById("job-actions"),
  jobRetry: document.getElementById("job-retry"),
  jobChooseDifferent: document.getElementById("job-choose-different"),

  viewLibrary: document.getElementById("view-library"),
  libraryFilters: document.getElementById("library-filters"),
  libraryStatus: document.getElementById("library-status"),
  libraryGrid: document.getElementById("library-grid"),
  libraryEmpty: document.getElementById("library-empty"),
  libraryPrev: document.getElementById("library-prev"),
  libraryNext: document.getElementById("library-next"),
  libraryPageLabel: document.getElementById("library-page-label"),

  viewAdmin: document.getElementById("view-admin"),
  adminFilters: document.getElementById("admin-filters"),
  adminStatus: document.getElementById("admin-status"),
  adminTbody: document.getElementById("admin-tbody"),
  adminEmpty: document.getElementById("admin-empty"),
  adminPrev: document.getElementById("admin-prev"),
  adminNext: document.getElementById("admin-next"),
  adminPageLabel: document.getElementById("admin-page-label"),
  adminUsersStatus: document.getElementById("admin-users-status"),
  adminUsersTbody: document.getElementById("admin-users-tbody"),
  adminUsersMore: document.getElementById("admin-users-more"),

  viewEdit: document.getElementById("view-edit"),
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
  editProcess: document.getElementById("edit-process"),
  editHint: document.getElementById("edit-hint"),
};

const STATUS_LABEL = { queued: "Queued", processing: "Processing", done: "Done", failed: "Failed" };

// ---------------------------------------------------------------------------
// Identity. The session cookie is HttpOnly, so this page cannot read it and
// cannot tell whether it is signed in by looking -- it has to ask the server.
// That is what /auth/me is for, and it is why signing out is a request rather
// than deleting a cookie here.
// ---------------------------------------------------------------------------

let registering = false;
let currentUser = null;

function setAuthStatus(message) {
  el.authStatus.textContent = message;
  el.authStatus.hidden = false;
}

function applyAuthMode() {
  el.authHeading.textContent = registering ? "Create your account" : "Welcome back";
  el.authSubtext.textContent = registering
    ? "Start uploading in a minute."
    : "Sign in to your account.";
  el.authSubmit.textContent = registering ? "Create account" : "Sign in";
  el.authToggleText.textContent = registering
    ? "Already have an account?"
    : "New to Flickpond?";
  el.authToggle.textContent = registering ? "Sign in" : "Create an account";
  el.authPasswordHint.hidden = !registering;
}

function showSignedIn(user) {
  currentUser = user;
  el.who.textContent = user.email;
  el.operatorTag.hidden = user.role !== "operator";
  el.navAdmin.hidden = user.role !== "operator";
  el.auth.hidden = true;
  el.app.hidden = false;
  switchView("upload");
  renderResumePrompt();
}

function showSignedOut() {
  currentUser = null;
  el.auth.hidden = false;
  el.app.hidden = true;
  stopPolling();
  el.activeJob.hidden = true;
  // Signing out is not just hiding the page: a player left running behind the
  // sign-in form would keep streaming a video the user can no longer see.
  unmountPlayersIn(el.app);
  el.player.hidden = true;
}

async function refreshIdentity() {
  try {
    const res = await fetch(`${API}/auth/me`);
    if (res.ok) {
      showSignedIn(await res.json());
      return true;
    }
  } catch {
    // Fall through: an unreachable API looks the same as being signed out,
    // and the sign-in form is the useful thing to show either way.
  }
  showSignedOut();
  return false;
}

el.authToggle.addEventListener("click", () => {
  registering = !registering;
  applyAuthMode();
  el.authStatus.hidden = true;
});

el.authForm.addEventListener("submit", async (event) => {
  event.preventDefault();
  el.authSubmit.disabled = true;
  const path = registering ? "/auth/register" : "/auth/login";
  try {
    const res = await fetch(`${API}${path}`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ email: el.authEmail.value, password: el.authPassword.value }),
    });
    if (res.ok) {
      el.authPassword.value = "";
      showSignedIn(await res.json());
      return;
    }
    if (res.status === 409) {
      setAuthStatus("That email is already registered.");
    } else if (res.status === 401) {
      // The server does not say which half was wrong, and neither does this.
      setAuthStatus("Incorrect email or password.");
    } else if (res.status === 422) {
      setAuthStatus("Enter a valid email and a password of at least 8 characters.");
    } else {
      setAuthStatus(`Sign in failed: ${res.status}`);
    }
  } catch (err) {
    setAuthStatus(`Request failed: ${err.message}`);
  } finally {
    el.authSubmit.disabled = false;
  }
});

el.logout.addEventListener("click", async () => {
  await fetch(`${API}/auth/logout`, { method: "POST" }).catch(() => {});
  showSignedOut();
});

// ---------------------------------------------------------------------------
// View switching. A small in-page app shell -- no router, three sections
// toggled with `hidden`. Admin is a real guard, not just a hidden nav link:
// the tab does not exist for a non-operator, and switching there by any other
// means still refuses to render it (the API would 403 anyway, but the point
// is never to ask).
// ---------------------------------------------------------------------------

const NAV_BUTTONS = { upload: el.navUpload, library: el.navLibrary, admin: el.navAdmin };
// The editor has no nav button of its own: it is opened from a job, not
// browsed to, and the nav entry it belongs to is Library.
const VIEWS = {
  upload: el.viewUpload,
  library: el.viewLibrary,
  admin: el.viewAdmin,
  edit: el.viewEdit,
};

function switchView(name) {
  if (name === "admin" && (!currentUser || currentUser.role !== "operator")) {
    name = "upload";
  }
  // Leaving the editor ends the session: its player would otherwise keep
  // streaming a video that is no longer on screen, and the components it
  // mounted would keep their own listeners alive.
  if (name !== "edit") closeEditor();
  for (const [key, section] of Object.entries(VIEWS)) {
    section.hidden = key !== name;
  }
  for (const [key, button] of Object.entries(NAV_BUTTONS)) {
    // The editor has no nav button; it is reached from Library, so Library
    // stays lit while it is open rather than leaving the nav with nothing on.
    button.classList.toggle("active", key === name || (name === "edit" && key === "library"));
  }
  if (name === "library") void loadLibrary();
  if (name === "admin") {
    void loadAdmin();
    void loadAdminUsers();
  }
}

el.navUpload.addEventListener("click", () => switchView("upload"));
el.navLibrary.addEventListener("click", () => switchView("library"));
el.navAdmin.addEventListener("click", () => switchView("admin"));

// ---------------------------------------------------------------------------
// Upload + the active job's status. One job is tracked at a time: starting a
// new upload always wins over whatever the previous one was doing (P4's
// escape hatch is choosing a different file, not waiting forever).
// ---------------------------------------------------------------------------

let lastFile = null;
let uploading = false;
let activeJobId = null;
// The last job document the upload card was showing, so its Edit button has
// something to hand the editor.
let activeJobDoc = null;

const POLL_INTERVAL_MS = 2000;
const PROGRESS_LABEL = { queued: "Queued...", processing: "Processing..." };
// 30 polls is a minute. The transcode step finishes well inside that under
// normal load, so past this something is wrong -- most likely a worker that
// died holding the job, which leaves the row at `processing` with nobody left
// to write to it. Keep polling anyway: the page cannot tell "stuck" from
// "slow", and saying it failed would be a claim we cannot support. Say it is
// taking long, hand the form back, and let the user decide.
const SLOW_AFTER_POLLS = 30;
// Overnight the worker pool scales to zero (docs/sprint5-plan.md), so the
// first job waits for a spot instance to boot. Past ten seconds in the queue,
// say so -- "Queued..." for three minutes reads as broken.
const COLD_START_AFTER_POLLS = 5;
const COLD_START_LABEL = "Queued — starting a worker, this can take a few minutes...";

/** What to say about a job still in flight, `attempt` polls in. */
function progressLabel(job, attempt) {
  if (job.status === "queued" && attempt >= COLD_START_AFTER_POLLS) return COLD_START_LABEL;
  // How far the MP4 has got, when the worker has said (sprint 6). Absent
  // means not yet known, not zero.
  if (job.status === "processing" && Number.isInteger(job.progress)) {
    return `Processing... ${job.progress}%`;
  }
  return PROGRESS_LABEL[job.status] ?? job.status;
}

function setStatus(message) {
  el.status.textContent = message;
  el.status.hidden = false;
}

// One follower per surface. The upload card and the editor watch different
// jobs, so starting an upload must not stop an edit that is still running.
// The counter above each timer is what makes an abandoned poll harmless: a
// response that arrives after its follower was replaced is dropped rather
// than drawn over whatever replaced it.
const pollTimers = { upload: null, edit: null };
const pollRuns = { upload: 0, edit: 0 };

function stopPolling(scope) {
  for (const key of scope ? [scope] : Object.keys(pollTimers)) {
    pollRuns[key] += 1;
    if (pollTimers[key] !== null) {
      clearTimeout(pollTimers[key]);
      pollTimers[key] = null;
    }
  }
}

/** Follow one job until it stops changing, handing every response to the
 *  caller. Both surfaces want the same loop with different chrome around it,
 *  so the loop lives here and the chrome is passed in.
 *
 * Handlers: onJob(job, attempt) for every response, then exactly one of
 * onDone(job) / onFailed(job), or onError(err) if the request itself fails. */
function followJob(scope, jobId, handlers) {
  const run = ++pollRuns[scope];

  const tick = async (attempt) => {
    pollTimers[scope] = null;

    let job;
    try {
      const res = await fetch(`${API}/jobs/${jobId}`);
      if (res.status === 401) {
        showSignedOut();
        setAuthStatus("Your session expired. Sign in again.");
        return;
      }
      job = await res.json();
    } catch (err) {
      if (pollRuns[scope] !== run) return;
      handlers.onError(err);
      return; // Stop on network errors to avoid a tight loop.
    }

    if (pollRuns[scope] !== run) return; // Replaced while this was in flight.
    handlers.onJob(job, attempt);

    if (job.status === "done") {
      // A done job whose ladder is still being built is not finished with us.
      // The MP4 is playable this second and the adaptive version is still
      // worth waiting for, so the page stays on the job until `hls_status`
      // settles. That field is the API's answer to "is more coming", and it
      // is always present -- inferring it from a missing `hls_url` cannot
      // tell "still coming" from "never coming".
      if (job.hls_status === "pending") {
        pollTimers[scope] = setTimeout(() => tick(attempt + 1), POLL_INTERVAL_MS);
        return;
      }
      handlers.onDone(job);
      return;
    }
    if (job.status === "failed") {
      handlers.onFailed(job);
      return;
    }

    pollTimers[scope] = setTimeout(() => tick(attempt + 1), POLL_INTERVAL_MS);
  };

  void tick(0);
}

function setBusy(busy) {
  uploading = busy;
  el.dropzone.classList.toggle("busy", busy);
  el.fileInput.disabled = busy;
}

function resetJobCard() {
  el.activeJob.hidden = true;
  el.jobProgress.hidden = true;
  el.jobActions.hidden = true;
  el.jobEdit.hidden = true;
  activeJobDoc = null;
  clearUploadPlayer();
}

function setBadge(status) {
  el.jobBadge.textContent = STATUS_LABEL[status] ?? status;
  el.jobBadge.className = `badge badge-${status in STATUS_LABEL ? status : "queued"}`;
}

el.dropzone.addEventListener("click", (event) => {
  if (event.target !== el.browseButton) el.fileInput.click();
});
el.browseButton.addEventListener("click", () => el.fileInput.click());

el.dropzone.addEventListener("dragover", (event) => {
  event.preventDefault();
  el.dropzone.classList.add("over");
});
el.dropzone.addEventListener("dragleave", () => el.dropzone.classList.remove("over"));
el.dropzone.addEventListener("drop", (event) => {
  event.preventDefault();
  el.dropzone.classList.remove("over");
  const file = event.dataTransfer?.files?.[0];
  if (file) void handleFile(file);
});
el.fileInput.addEventListener("change", () => {
  const file = el.fileInput.files[0];
  if (file) void handleFile(file);
});

el.jobRetry.addEventListener("click", () => {
  if (lastFile) void handleFile(lastFile);
});
el.jobChooseDifferent.addEventListener("click", () => el.fileInput.click());
el.jobDelete.addEventListener("click", deleteActiveJob);

el.resumeChoose.addEventListener("click", () => el.fileInput.click());
el.resumeDiscard.addEventListener("click", async () => {
  const record = offeredUpload;
  // Forget it first: the local note is what the reader is asking to be rid of,
  // and it should go even if the request below never lands.
  offeredUpload = null;
  if (record) forgetPendingUpload(record.uploadId);
  renderResumePrompt();
  if (!record) return;
  try {
    await fetch(`${API}/uploads/${record.uploadId}`, { method: "DELETE" });
  } catch {
    // The parts are collected when the session expires either way.
  }
});

el.uploadCancel.addEventListener("click", cancelUpload);

async function deleteActiveJob() {
  if (!activeJobId) return;
  const filename = el.jobFilename.textContent || "this video";
  if (!window.confirm(`Delete "${filename}"? This cannot be undone.`)) return;

  const jobId = activeJobId;
  try {
    const res = await fetch(`${API}/jobs/${jobId}`, { method: "DELETE" });

    if (res.status === 401) {
      showSignedOut();
      setAuthStatus("Your session expired. Sign in again.");
      return;
    }

    if (!res.ok && res.status !== 204) {
      const data = await res.json().catch(() => ({}));
      setStatus(`Could not delete "${filename}": ${data.error ?? res.status}`);
      return;
    }

    // Deleting is safe at any status, including mid-poll (DELETE /jobs/{id}
    // works regardless) -- stop polling for a job that no longer exists.
    stopPolling();
    resetJobCard();
    activeJobId = null;
    lastFile = null;
    setStatus("Video deleted.");
  } catch (err) {
    setStatus(`Request failed: ${err.message}`);
  }
}

// --- what the API will and will not take -----------------------------------

let limits = null;
let limitsPromise = null;

/** The API's limits, fetched once per page load.
 *
 * `/limits` is public and changes only when the deployment does. Null means
 * the server has no such route -- anything from before sprint 4 -- and the
 * page then says nothing about a limit it cannot know, rather than inventing
 * one.
 */
function loadLimits() {
  limitsPromise ??= fetch(`${API}/limits`)
    .then((res) => (res.ok ? res.json() : null))
    .catch(() => null)
    .then((value) => {
      limits = value;
      return value;
    });
  return limitsPromise;
}

/** A limit the way a person would say it: "5 minutes", "90 seconds". */
function describeLimit(seconds) {
  if (seconds % 60 === 0) {
    const minutes = seconds / 60;
    return `${minutes} minute${minutes === 1 ? "" : "s"}`;
  }
  return `${seconds} seconds`;
}

/** A size limit the way a person would say it: "2 GB", not "2.0 GB". */
function describeSize(bytes) {
  const gib = 1024 ** 3;
  return bytes % gib === 0 ? `${bytes / gib} GB` : formatBytes(bytes);
}

/** Say the duration limit next to the picker, when there is one.
 *
 * The size figure that used to live here is gone with the limit it described:
 * a file the browser can type now goes straight to storage in parts, so
 * quoting 100 MB would be wrong for almost every upload. A file it cannot type
 * still goes through the one-shot endpoint, which still stops at 100 MB, and
 * `sendWholeFile` is where that is said. */
function applyLimitHint() {
  const bounds = [];
  if (limits?.max_duration_seconds > 0) bounds.push(describeLimit(limits.max_duration_seconds));
  if (limits?.max_upload_bytes > 0) bounds.push(describeSize(limits.max_upload_bytes));
  if (bounds.length > 0) {
    el.dropzoneHint.textContent = `MP4, MOV, WebM, MKV, or AVI, up to ${bounds.join(" and ")}.`;
  }
}

/** How long a file is, read in the browser before anything is uploaded.
 *
 * The worker measures the real duration with ffprobe and refuses an over-long
 * video there, but that is minutes late for a large file. This is the courtesy
 * that says so first. `null` means it could not be read -- a browser with no
 * object URLs, a container it cannot demux, metadata that never arrives -- and
 * the upload proceeds, so a file the browser cannot read is never blocked. */
function readDurationSeconds(file) {
  if (typeof URL.createObjectURL !== "function") return Promise.resolve(null);

  return new Promise((resolve) => {
    const url = URL.createObjectURL(file);
    const probe = document.createElement("video");

    const finish = (seconds) => {
      URL.revokeObjectURL(url);
      resolve(Number.isFinite(seconds) && seconds > 0 ? seconds : null);
    };

    probe.preload = "metadata";
    probe.addEventListener("loadedmetadata", () => finish(probe.duration));
    probe.addEventListener("error", () => finish(null));
    probe.src = url;
  });
}

/** Whether this file can take the direct path at all.
 *
 * The direct path has to *declare* a content type and the API stores only the
 * ones on its list, so a file the browser could not type -- an .mkv on most
 * Linux desktops, an .avi on Windows -- has nothing to declare. Those still go
 * through the legacy one-shot POST, which identifies the container from the
 * bytes instead of trusting the declaration. */
function canUploadDirectly(file) {
  return Boolean(file.type) && ALLOWED_CONTENT_TYPES.has(file.type);
}

/** An error that carries its status, so the caller can tell "your session
 *  ended" from "the upload failed" without matching on message text. */
function httpError(status, data) {
  const error = new Error(data?.error ?? `HTTP ${status}`);
  error.status = status;
  return error;
}

// --- the readout -----------------------------------------------------------

// A readout that averaged the whole transfer would keep reporting the slow
// start long after the connection settled. Eight seconds is short enough to
// follow a change and long enough that one slow part does not swing it.
const RATE_WINDOW_MS = 8000;

function makeRateMeter(now = () => Date.now()) {
  const samples = [];

  return {
    /** Record that `bytes` have landed. */
    mark(bytes) {
      const at = now();
      samples.push({ at, bytes });
      while (samples.length > 2 && at - samples[0].at > RATE_WINDOW_MS) samples.shift();
    },
    /** Bytes per second across the window, or null until two points exist. */
    rate() {
      if (samples.length < 2) return null;
      const first = samples[0];
      const last = samples[samples.length - 1];
      const seconds = (last.at - first.at) / 1000;
      if (seconds <= 0) return null;
      return (last.bytes - first.bytes) / seconds;
    },
  };
}

const BYTE_UNITS = ["B", "KB", "MB", "GB", "TB"];

function formatBytes(bytes) {
  let value = bytes;
  let unit = 0;
  while (value >= 1024 && unit < BYTE_UNITS.length - 1) {
    value /= 1024;
    unit += 1;
  }
  return `${value.toFixed(unit === 0 || value >= 10 ? 0 : 1)} ${BYTE_UNITS[unit]}`;
}

function formatEta(seconds) {
  if (!Number.isFinite(seconds) || seconds < 0) return "";
  if (seconds < 1) return "almost done";
  if (seconds < 60) return `about ${Math.round(seconds)}s left`;
  return `about ${Math.round(seconds / 60)} min left`;
}

function showUploadProgress(sent, total, meter) {
  const percent = total > 0 ? Math.min(100, Math.round((sent / total) * 100)) : 0;
  el.uploadProgress.hidden = false;
  el.uploadProgressFill.style.width = `${percent}%`;
  el.uploadProgressTrack.setAttribute("aria-valuenow", String(percent));
  el.uploadProgressPercent.textContent = `${percent}%`;

  // The rate needs two measurements to exist, so the first part shows a
  // percentage and nothing else rather than a made-up speed.
  const rate = meter.rate();
  el.uploadProgressRate.textContent = rate === null ? "" : `${formatBytes(rate)}/s`;
  el.uploadProgressEta.textContent = rate === null ? "" : formatEta((total - sent) / rate);
}

function clearUploadProgress() {
  el.uploadProgress.hidden = true;
  el.uploadCancel.hidden = false;
  el.uploadProgressFill.style.width = "0%";
  el.uploadProgressTrack.setAttribute("aria-valuenow", "0");
  el.uploadProgressPercent.textContent = "0%";
  el.uploadProgressRate.textContent = "";
  el.uploadProgressEta.textContent = "";
}

// --- the direct path -------------------------------------------------------

/** How many parts are in the air at once.
 *
 * One at a time keeps the readout and the retry story trivial, but on a
 * high-latency link most of a part's time is round trips rather than bytes, and
 * a single stream never fills the connection (#68). Three uses it without
 * leaving much work behind if the page goes away mid-transfer. */
const PARTS_IN_FLIGHT = 3;

/** Thrown when the reader cancels.
 *
 * An outcome, not a failure: it is what keeps a cancelled upload from being
 * reported the way a dropped connection is. */
class UploadCancelled extends Error {
  constructor() {
    super("upload cancelled");
    this.name = "UploadCancelled";
    this.cancelled = true;
  }
}

/** The transfer Cancel would stop, or null when none is running. One slot is
 *  enough: `handleFile` refuses a second file while the first is still going. */
let activeUpload = null;

function beginUploadControl(uploadId) {
  activeUpload = { uploadId, cancelled: false, controller: new AbortController() };
  return activeUpload;
}

/** Stop the upload in progress: the part in flight, the parts behind it, and
 *  the session on the server.
 *
 * The DELETE is what makes it real -- the parts already in storage are
 * otherwise collected only when the session expires, a day later -- so it goes
 * even though the page has stopped waiting for this transfer. A refused or
 * unreachable DELETE leaves the reader no worse off than a tab that crashed
 * mid-upload, which is the state this page exists to recover from anyway. */
async function cancelUpload() {
  const control = activeUpload;
  if (!control || control.cancelled) return; // one cancel per transfer
  control.cancelled = true;
  control.controller.abort();
  forgetPendingUpload(control.uploadId);
  renderResumePrompt();
  try {
    await fetch(`${API}/uploads/${control.uploadId}`, { method: "DELETE" });
  } catch {
    // The parts are collected when the session expires either way.
  }
}

/** Open a multipart session, or null when this API has no usable `/uploads`.
 *
 * Two things mean that, and they mean the same thing to the caller: a server
 * from before sprint 4 answers 404, and a body with no session in it is no
 * more usable than no route at all. Either way the upload falls back to the
 * one-shot POST rather than half-starting a transfer it cannot finish. */
/** The frame-rate ceiling for this upload: 30 unless the reader kept 60.
 *
 * Read when the upload starts, so ticking the box mid-transfer changes the
 * next upload, not this one. A resumed upload keeps the choice it was
 * started with -- the server stored it with the session. */
function chosenMaxFps() {
  return el.keep60fps?.checked ? 60 : 30;
}

async function openUploadSession(file) {
  const res = await fetch(`${API}/uploads`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({
      filename: file.name,
      size: file.size,
      content_type: file.type,
      max_fps: chosenMaxFps(),
    }),
  });
  if (res.status === 404) return null;
  const data = await res.json().catch(() => ({}));
  if (!res.ok) throw httpError(res.status, data);
  if (!data.upload_id || !data.part_size || !data.part_count) return null;
  return data; // { upload_id, part_size, part_count }
}

/** Presigned URLs are short-lived, so each part is signed as it is about to
 *  be sent rather than all of them up front. */
async function signPart(uploadId, number) {
  const res = await fetch(`${API}/uploads/${uploadId}/parts`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ part_numbers: [number] }),
  });
  const data = await res.json().catch(() => ({}));
  if (!res.ok) throw httpError(res.status, data);
  const url = data.urls?.[String(number)];
  if (!url) throw new Error(`the API did not sign part ${number}`);
  return url;
}

// A part that fails for good ends the whole upload -- resumable, but only
// after the reader picks the file again, so one dropped connection on part 40
// of 64 costs more than the retry it did not get. Each part tries three times,
// a second then two apart. Only failures that can come out differently next
// time are retried: the network (fetch rejects), a 5xx, 408 and 429. A 403 is
// an expired or refused URL, and the same request would be refused the same
// way.
const PART_ATTEMPTS = 3;
const PART_RETRY_DELAY_MS = 1000;

function isWorthRetrying(status) {
  return status >= 500 || status === 408 || status === 429;
}

const pause = (ms) => new Promise((resolve) => setTimeout(resolve, ms));

async function putPartWithRetries(url, blob, control) {
  for (let attempt = 1; ; attempt += 1) {
    const last = attempt === PART_ATTEMPTS;
    try {
      const res = await fetch(url, {
        method: "PUT",
        body: blob,
        credentials: "omit",
        // Cancel aborts the part in flight rather than waiting out a transfer
        // that can take minutes to notice the reader has given up on it.
        signal: control.controller.signal,
      });
      if (res.ok || last || !isWorthRetrying(res.status)) return res;
    } catch (err) {
      // A connection the reader aborted is their own doing: nothing to retry,
      // and nothing to report as a failure.
      if (control.cancelled) throw new UploadCancelled();
      if (last) throw new Error(`the connection dropped while sending a part (${err.message})`);
    }
    await pause(PART_RETRY_DELAY_MS * attempt);
  }
}

/** PUT one part straight to storage and hand back its ETag.
 *
 * The API cookie must not follow this request: storage is a different service
 * on a different origin and has no use for it. The ETag only comes back at all
 * if the bucket's CORS rules list it under exposed headers, and without that
 * every part succeeds and completion fails for a reason that looks nothing
 * like the cause -- so it is worth naming. */
async function putPart(url, blob, control) {
  const res = await putPartWithRetries(url, blob, control);
  if (!res.ok) throw new Error(`storage rejected a part (HTTP ${res.status})`);
  const etag = res.headers.get("ETag");
  if (!etag) {
    throw new Error("storage did not expose the part's ETag (the bucket's CORS must list it)");
  }
  return etag;
}

async function finishUploadSession(uploadId, parts) {
  const res = await fetch(`${API}/uploads/${uploadId}/complete`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ parts }),
  });
  const data = await res.json().catch(() => ({}));
  if (!res.ok) throw httpError(res.status, data);
  return data.job_id;
}

/** Send the parts the server does not already hold, `PARTS_IN_FLIGHT` at a
 *  time, and hand back the manifest in part order.
 *
 * Parts are independent -- any of them may go up in any order and at any
 * time, and only completion cares that they are all there -- so the pool is
 * over part numbers rather than over the file. `held` is what a resumed upload
 * was told is already in storage; those bytes are not sent twice. The manifest
 * comes back in order because that is what the API checks against storage. */
async function sendParts({ uploadId, partSize, partCount, file, control, held, onSent }) {
  const etags = new Array(partCount);
  const todo = [];
  for (let n = 1; n <= partCount; n += 1) {
    const already = held.get(n);
    if (already === undefined) todo.push(n);
    else etags[n - 1] = already;
  }

  let next = 0;
  const sendOne = async () => {
    while (next < todo.length) {
      // Checked between parts, not only on the fetch: a part whose bytes were
      // already on their way when Cancel was pressed still lands, and nothing
      // should follow it.
      if (control.cancelled) throw new UploadCancelled();
      const n = todo[next];
      next += 1;
      const start = (n - 1) * partSize;
      const blob = file.slice(start, Math.min(start + partSize, file.size));
      etags[n - 1] = await putPart(await signPart(uploadId, n), blob, control);
      onSent(blob.size);
    }
  };

  const sends = Math.min(PARTS_IN_FLIGHT, todo.length);
  await Promise.all(Array.from({ length: sends }, sendOne));
  return etags.map((etag, index) => ({ n: index + 1, etag }));
}

/** Send what is missing, then ask the API to assemble the parts into a job.
 *
 * `held` is what the server already has -- nothing for a fresh upload, the
 * parts a reloaded page was told about for a resumed one -- and it is where
 * the readout starts, so a resumed upload does not count the same bytes
 * twice. */
async function transferParts({ uploadId, partSize, partCount, file, held = new Map() }) {
  const control = beginUploadControl(uploadId);
  const meter = makeRateMeter();
  let sent = 0;
  for (const n of held.keys()) sent += partBytes(n, partSize, file.size);
  showUploadProgress(sent, file.size, meter);

  try {
    const parts = await sendParts({
      uploadId,
      partSize,
      partCount,
      file,
      control,
      held,
      onSent(bytes) {
        sent += bytes;
        meter.mark(sent);
        showUploadProgress(sent, file.size, meter);
      },
    });
    // Every part is in storage, so Cancel has nothing left to stop: the button
    // goes, and the bar stays where it is -- at all the bytes -- while the API
    // assembles the parts into a job.
    activeUpload = null;
    el.uploadCancel.hidden = true;
    const jobId = await finishUploadSession(uploadId, parts);
    // The session has become a job; there is nothing left to come back to.
    forgetPendingUpload(uploadId);
    return jobId;
  } finally {
    // A failed or cancelled transfer leaves nothing for the button to stop.
    activeUpload = null;
  }
}

/** Upload a file the browser could name straight to storage. */
async function runDirectUpload(session, file) {
  return transferParts({
    uploadId: session.upload_id,
    partSize: session.part_size,
    partCount: session.part_count,
    file,
  });
}

/** One opaque POST. Still the only path that can take a file whose type the
 *  browser could not name. */
async function sendWholeFile(file) {
  if (file.size > MAX_FILE_SIZE_BYTES) {
    throw new Error(
      `this server has no direct upload, and its one-shot upload stops at ${formatBytes(MAX_FILE_SIZE_BYTES)}`,
    );
  }
  const body = new FormData();
  body.append("file", file);
  body.append("max_fps", String(chosenMaxFps()));
  const res = await fetch(`${API}/upload`, { method: "POST", body });
  const data = await res.json().catch(() => ({}));
  if (!res.ok) throw httpError(res.status, data);
  return data.job_id;
}

// --- resuming an interrupted upload ----------------------------------------

// A pointer to an unfinished session, kept between page loads. The session and
// the parts already in storage live on the server for 24 hours; this is only
// how the next page load finds them again.
//
// One key per upload, not one for the browser: with a single key, a second tab
// starting an upload silently replaced the first tab's note, and the session
// it pointed at then sat unused until the server expired it (#69).
const PENDING_UPLOAD_PREFIX = "flickpond.pending-upload.";
// Sprint 4 kept its one note under this key. It is read once and moved, so an
// upload interrupted by the deploy that split the notes is still offered.
const LEGACY_PENDING_UPLOAD_KEY = "flickpond.pending-upload";
// The server's own session life, and so the point past which a note points at
// nothing a resume could use.
const PENDING_UPLOAD_TTL_MS = 24 * 60 * 60 * 1000;

/** The note stored under `key`, or null when there is none, when it cannot be
 *  read, or when what is there is not a note.
 *
 * localStorage throws when it is full, and in a browser that has been told to
 * keep nothing. None of that should fail an upload, so every read and write
 * here shrugs and carries on. */
function readNote(key) {
  try {
    const raw = localStorage.getItem(key);
    return raw ? JSON.parse(raw) : null;
  } catch {
    return null;
  }
}

function forgetKey(key) {
  try {
    localStorage.removeItem(key);
  } catch {
    // Same.
  }
}

/** Every note this page has left in this browser. Guarded like the reads
 *  below: enumerating storage is one more thing a locked-down browser can
 *  refuse, and refusing it is not a reason to lose the page. */
function pendingUploadKeys() {
  try {
    return Object.keys(localStorage).filter((key) => key.startsWith(PENDING_UPLOAD_PREFIX));
  } catch {
    return [];
  }
}

/** Move sprint 4's single note under a key of its own, so the rest of this
 *  section only ever has to look in one place. */
function adoptLegacyPendingUpload() {
  const legacy = readNote(LEGACY_PENDING_UPLOAD_KEY);
  if (!legacy) return;
  forgetKey(LEGACY_PENDING_UPLOAD_KEY);
  savePendingUpload(legacy);
}

/** Every unfinished upload this browser still knows about, newest first.
 *
 * A note whose session the server has already expired is dropped rather than
 * offered: the resume would 404 and the file would go up from the start
 * anyway, only after the reader answered a pointless prompt. */
function pendingUploads() {
  adoptLegacyPendingUpload();
  const notes = [];
  for (const key of pendingUploadKeys()) {
    const note = readNote(key);
    const fresh =
      note && Number.isFinite(note.startedAt) && Date.now() - note.startedAt < PENDING_UPLOAD_TTL_MS;
    if (fresh) {
      notes.push(note);
    } else {
      // Unreadable, not a note, or pointing at a session that has expired.
      forgetKey(key);
    }
  }
  return notes.sort((a, b) => b.startedAt - a.startedAt);
}

function savePendingUpload(record) {
  try {
    localStorage.setItem(PENDING_UPLOAD_PREFIX + record.uploadId, JSON.stringify(record));
  } catch {
    // The upload still works; it just cannot be picked up again later.
  }
}

/** Forget one upload's note, and only that one: another tab's unfinished
 *  upload is none of this page's business. */
function forgetPendingUpload(uploadId) {
  forgetKey(PENDING_UPLOAD_PREFIX + uploadId);
}

/** Whether the file just picked is the one this record was made from.
 *
 * The server cannot prove it either: a name, a length and a modification time
 * are all a browser hands back for a file it has already let go of. This is a
 * courtesy check that the reader picked the same file, not a promise about
 * what is inside it. */
function isSameFile(record, file) {
  return (
    record.filename === file.name &&
    record.size === file.size &&
    record.lastModified === file.lastModified
  );
}

/** How many bytes part `n` of a `total`-byte file holds. */
function partBytes(n, partSize, total) {
  return Math.min(partSize, total - (n - 1) * partSize);
}

/** Whether a note belongs to the account signed in here now. The server would
 *  refuse anyone else's session anyway; this is what keeps the page from
 *  offering it. */
function isOwnUpload(note) {
  return Boolean(currentUser && note.userId === currentUser.id);
}

/** The unfinished session this file can complete, if there is one.
 *
 * Three things have to line up, or this is an ordinary upload: the same
 * account, the same file, and -- checked by asking, a moment later -- a
 * session the server still has. Two notes for the same file are the same file
 * twice: the newest one wins. */
function resumableRecordFor(file) {
  return pendingUploads().find((note) => isOwnUpload(note) && isSameFile(note, file)) ?? null;
}

/** The note the prompt is currently offering, so the Discard button throws
 *  away the one the reader is looking at rather than whichever is newest. */
let offeredUpload = null;

/** Offer to finish an upload this browser did not get to the end of.
 *
 * Never offered to a different account: the note names whoever started it, and
 * the server would refuse it to anyone else anyway. The newest unfinished
 * upload is the one named; an older one is still picked up when its file is
 * chosen, which is the only way it could be resumed at all. */
function renderResumePrompt() {
  offeredUpload = pendingUploads().find(isOwnUpload) ?? null;
  el.resumePrompt.hidden = !offeredUpload;
  if (offeredUpload) {
    el.resumeText.textContent =
      `Unfinished upload: ${offeredUpload.filename}. Choose the same file to pick up where it stopped.`;
  }
}

/** What the server already holds of a session. Null when it holds nothing:
 *  expired, cleaned up, or belonging to someone else. */
async function getUploadProgress(uploadId) {
  const res = await fetch(`${API}/uploads/${uploadId}`);
  if (!res.ok) return null;
  return await res.json().catch(() => null);
}

/** Open a session for this file and note it down, so a reload can find it. */
async function startDirectUpload(file) {
  const session = await openUploadSession(file);
  if (!session) return null;
  savePendingUpload({
    uploadId: session.upload_id,
    partSize: session.part_size,
    partCount: session.part_count,
    userId: currentUser?.id ?? null,
    filename: file.name,
    size: file.size,
    lastModified: file.lastModified,
    startedAt: Date.now(),
  });
  return session;
}

/** Finish a session this browser started before the page was reloaded.
 *
 * The server is the authority on what is already in storage -- the page's own
 * memory of it went with the reload -- so this asks first and then sends only
 * the parts that are missing. A session the server has forgotten, or one that
 * has been cancelled, is not an error worth showing: the file is simply
 * uploaded from the beginning. */
async function resumeDirectUpload(record, file) {
  const progress = await getUploadProgress(record.uploadId);

  if (progress === null || progress.state === "aborting" || progress.state === "aborted") {
    forgetPendingUpload(record.uploadId);
    const session = await startDirectUpload(file);
    return session ? runDirectUpload(session, file) : sendWholeFile(file);
  }

  // The server got all the way to a job. There is nothing left to send.
  if (progress.state === "completed") {
    forgetPendingUpload(record.uploadId);
    return progress.job_id;
  }

  return transferParts({
    uploadId: record.uploadId,
    partSize: progress.part_size,
    partCount: progress.part_count,
    file,
    held: new Map(progress.parts_done.map((part) => [part.n, part.etag])),
  });
}

async function sendVideo(file) {
  if (canUploadDirectly(file)) {
    // A file this browser was already part way through goes back to its own
    // session rather than starting a second one.
    const record = resumableRecordFor(file);
    if (record) return resumeDirectUpload(record, file);

    const session = await startDirectUpload(file);
    if (session) return runDirectUpload(session, file);
  }
  return sendWholeFile(file);
}

async function handleFile(file) {
  if (uploading) return; // A rapid second drop cannot double-post the first.

  clearUploadProgress();

  if (file.type && !ALLOWED_CONTENT_TYPES.has(file.type)) {
    setStatus(`"${file.type}" is not a video format we handle.`);
    return;
  }
  // The 100 MB limit belongs to the legacy one-shot endpoint. The direct path
  // streams parts to storage instead and its own bound is the API's part count,
  // so refusing a large file here would refuse one the API accepts.
  if (!canUploadDirectly(file) && file.size > MAX_FILE_SIZE_BYTES) {
    setStatus(`That file is larger than the ${formatBytes(MAX_FILE_SIZE_BYTES)} limit.`);
    return;
  }
  // The direct path's own cap, from /limits: refused here before a single part
  // goes, instead of by a 413 once the session is asked for.
  const maxUpload = limits?.max_upload_bytes ?? 0;
  if (canUploadDirectly(file) && maxUpload > 0 && file.size > maxUpload) {
    setStatus(`That file is larger than the ${describeSize(maxUpload)} limit.`);
    return;
  }

  // A new upload always wins over whatever the previous job was doing.
  stopPolling();
  resetJobCard();
  activeJobId = null;
  lastFile = file;
  setBusy(true);
  setStatus("Uploading...");
  // The prompt is about a file the reader is no longer looking at.
  el.resumePrompt.hidden = true;

  try {
    // The worker refuses an over-long video once it has probed it, which is
    // minutes late for a large one. Reading the length here is what makes the
    // refusal instant, and "could not read it" falls through to the worker
    // rather than blocking a file the browser does not understand.
    const seconds = await readDurationSeconds(file);
    const durationLimit = limits?.max_duration_seconds ?? 0;
    if (durationLimit > 0 && seconds !== null && seconds > durationLimit) {
      setStatus(
        `That video is longer than ${describeLimit(durationLimit)} — the worker would refuse it. Trim it and try again.`,
      );
      setBusy(false);
      return;
    }

    const jobId = await sendVideo(file);

    clearUploadProgress();
    setStatus("Received, waiting for processing...");
    el.activeJob.hidden = false;
    el.jobFilename.textContent = file.name;
    setBadge("queued");
    activeJobId = jobId;
    setBusy(false);
    renderResumePrompt();
    pollUploadJob(jobId);
  } catch (err) {
    clearUploadProgress();
    renderResumePrompt();
    if (err.cancelled) {
      // Not a failure: the reader stopped it, and the card goes back to where
      // it was before a file was picked.
      setStatus("Upload cancelled.");
      setBusy(false);
      return;
    }
    if (err.status === 401) {
      showSignedOut();
      setAuthStatus("Your session expired. Sign in again.");
      setBusy(false);
      return;
    }
    if (err.status === 413) {
      // A server without /limits, or one whose cap changed since this page
      // loaded: still say it plainly, rather than "exceeds 2147483648 bytes".
      const maxUpload = limits?.max_upload_bytes ?? 0;
      setStatus(
        maxUpload > 0
          ? `That file is larger than the ${describeSize(maxUpload)} limit.`
          : "That file is larger than this server accepts.",
      );
      setBusy(false);
      return;
    }
    setStatus(`Upload failed: ${err.message}`);
    setBusy(false);
  }
}

/** The upload card's half of the loop: badge, progress sweep, and the two
 *  ways a job can end with the form still in the user's hands. */
function pollUploadJob(jobId) {
  followJob("upload", jobId, {
    onJob(job, attempt) {
      activeJobDoc = job;
      setBadge(job.status);
      el.jobEdit.hidden = job.status !== "done";
      el.jobProgress.hidden = job.status !== "processing";

      // Playable beats perfect: the MP4 goes up the moment the job says done,
      // and the note says whether an adaptive version is still on its way.
      if (job.status === "done") {
        showUploadPlayer(job);
        setStatus(
          job.hls_status === "pending"
            ? "Ready to play — HD versions are still processing."
            : "Processing complete.",
        );
        return;
      }

      if (attempt >= SLOW_AFTER_POLLS) {
        setStatus(`Still working on it — this is taking longer than expected. Job ${jobId}`);
        // Give the form back; a stuck job otherwise never releases it.
        el.jobActions.hidden = false;
      } else {
        setStatus(progressLabel(job, attempt));
      }
    },
    onDone(job) {
      showUploadPlayer(job);
      setStatus("Processing complete.");
    },
    onFailed(job) {
      setStatus(`Processing failed: ${job.error ?? "Unknown error"}`);
      clearUploadPlayer();
      el.jobActions.hidden = false;
    },
    onError(err) {
      setStatus(`Polling failed: ${err.message}`);
    },
  });
}

// ---------------------------------------------------------------------------
// Playback: Plyr for the control bar, hls.js for the adaptive ladder.
//
// Every <video> the app builds goes through mountPlayer() and is taken apart
// again by unmountPlayer(). That is not tidiness: Plyr keeps listeners on the
// element it was handed and hls.js keeps the element itself, so a Library card
// opened and closed ten times would leave ten of each running against elements
// nobody can see. Both libraries are also looked up at call time rather than
// assumed -- they are vendored files loaded by index.html, and a player that
// cannot load its library has to leave a plain playable <video> behind, not
// nothing at all.
// ---------------------------------------------------------------------------

// The one place "which URL do we play" is decided. The ladder is best-effort
// on the server, so its absence is a normal case, not an error.
function playbackSource(job) {
  if (job.hls_url) return { hls: job.hls_url, fallback: job.output_url ?? null };
  if (job.output_url) return { hls: null, fallback: job.output_url };
  return null;
}

const HLS_MIME = "application/vnd.apple.mpegurl";
const VENDOR_DIR = "vendor/";
// hls.js's own "no fixed level" value is -1, which Plyr would render as the
// menu entry "-1p". Auto gets its own sentinel and its own label instead.
const AUTO_QUALITY = 0;

// host element -> { video, plyr, hls }. Keyed by host because that is what the
// card owns; the <video> inside it does not outlive a Plyr destroy().
const players = new WeakMap();

/** Tear down whatever is mounted on `host` and empty it. Safe to call twice. */
function unmountPlayer(host) {
  const state = players.get(host);
  if (state) {
    players.delete(host);
    // hls.js first. Plyr.destroy() puts a *clone* of the media element back
    // into the DOM, so destroying hls.js afterwards would leave it holding an
    // element that is no longer the one on the page -- the leak this section
    // exists to prevent.
    // Both are cleared as well as destroyed: the quality menu this state built
    // outlives the player in whatever the browser still holds, and a callback
    // from it must find nothing to reach into.
    if (state.hls) {
      state.hls.destroy();
      state.hls = null;
    }
    if (state.plyr) {
      state.plyr.destroy();
      state.plyr = null;
    }
  }
  host.replaceChildren();
}

/** Take down every player inside `root` -- for the re-renders that throw away
 *  the cards holding them. */
function unmountPlayersIn(root) {
  for (const host of root.querySelectorAll(".player-host")) unmountPlayer(host);
}

/** The Upload tab's player slot, emptied. */
function clearUploadPlayer() {
  uploadPlayerKey = null;
  unmountPlayer(el.player);
  el.player.hidden = true;
}

/** What is in the Upload tab's player right now, so it is only rebuilt when
 *  the thing to play actually changes. */
let uploadPlayerKey = null;

/** Put the job's playable source in the upload card.
 *
 * The same job document arrives on every poll, so mounting unconditionally
 * would tear Plyr down and start playback over under the reader. Keying on
 * which job and which kind of source means the MP4 that goes up first is
 * replaced exactly once, when the ladder takes its place.
 *
 * Not on the URL: `output_url` is presigned, so it comes back with a new
 * signature on every poll, and keying on it rebuilt the player every two
 * seconds for as long as the ladder took -- the box collapsing and expanding.
 */
function showUploadPlayer(job) {
  if (!job.hls_url && !job.output_url) return;
  const key = `${job.id}:${job.hls_url ? "hls" : "mp4"}`;
  if (key === uploadPlayerKey) return;
  uploadPlayerKey = key;
  mountPlayer(el.player, job);
}

/** Put a player for `job` into `host`, replacing whatever was there. */
function mountPlayer(host, job, { autoplay = false } = {}) {
  unmountPlayer(host);

  const source = playbackSource(job);
  if (!source) {
    host.hidden = true;
    return null;
  }

  const video = document.createElement("video");
  // Native controls until Plyr replaces them, and for good if Plyr's file
  // never arrives. `preload=metadata` is what Plyr needs to size the box.
  video.controls = true;
  video.playsInline = true;
  video.preload = "metadata";
  if (autoplay) video.autoplay = true;

  host.replaceChildren(video);
  host.hidden = false;

  const state = { host, video, plyr: null, hls: null };
  players.set(host, state);

  // Only hand the ladder to something that can actually play it: hls.js
  // where MediaSource exists, the browser itself on an iPhone, and if neither
  // is available the MP4 is what the user gets.
  if (source.hls && attachHls(state, source)) return state;

  // Only a fallback that exists gets set: an empty src would have the element
  // request the page itself. A done job always has an MP4, so the guard is for
  // the case the API's own contract rules out.
  if (source.fallback) video.src = source.fallback;
  startPlyr(state, null);
  return state;
}

/** Returns true if the ladder took over the element. */
function attachHls(state, source) {
  const { video } = state;
  const Hls = window.Hls;

  // hls.js first wherever it runs, the browser's own player only where it
  // cannot (an iPhone has no MediaSource). Chrome now answers "maybe" for
  // HLS too, and its built-in player fails on this ladder -- error 4 on a
  // stream hls.js plays -- so "the browser says it can" is not enough.
  if (!Hls || !Hls.isSupported()) {
    if (!video.canPlayType(HLS_MIME)) return false;
    video.src = source.hls;
    // No level list to pass: Safari's own controls carry the quality menu.
    startPlyr(state, null);
    return true;
  }

  const hls = new Hls();
  state.hls = hls;

  hls.on(Hls.Events.MANIFEST_PARSED, () => {
    // Plyr builds its quality menu once, from the list it is given, so the
    // real level heights have to be known before the player is created --
    // which is exactly what waiting for this event buys.
    state.levelLabels = levelLabels(hls);
    startPlyr(state, levelHeights(hls));
  });

  hls.on(Hls.Events.ERROR, (_event, data) => {
    if (!data.fatal || !source.fallback) return;
    // Rebuild rather than repoint: this Plyr was built around the ladder's
    // level list, and its quality menu cannot be rebuilt in place. Deferred
    // because this runs inside hls.js's own dispatch, and guarded because the
    // card may have been collapsed while the failure was in flight.
    setTimeout(() => {
      if (players.get(state.host) !== state) return;
      mountPlayer(state.host, { output_url: source.fallback });
    }, 0);
  });

  hls.attachMedia(video);
  hls.loadSource(source.hls);
  return true;
}

/** The ladder's heights, highest first, Auto in front. Plyr's menu is built
 *  from this, so it never offers a rendition the video does not have. */
function levelHeights(hls) {
  const heights = hls.levels.map((level) => level.height).filter(Boolean);
  return [AUTO_QUALITY, ...[...new Set(heights)].sort((a, b) => b - a)];
}

/** "1080p60" for a rung the manifest says runs above 30 fps -- what a Keep
 *  60 fps upload gets (sprint 5). Plyr labels every other height "1080p" by
 *  itself, and badges 2160 as 4K. */
function levelLabels(hls) {
  const labels = {};
  for (const level of hls.levels) {
    if (level.height && Number(level.frameRate) > 30) labels[level.height] = `${level.height}p60`;
  }
  return labels;
}

/** Plyr takes an option click and hands the value back to us; this is where a
 *  chosen height becomes an hls.js level. */
function selectLevel(state, height) {
  const hls = state.hls;
  if (!hls) return;
  if (height === AUTO_QUALITY) {
    hls.currentLevel = -1;
    return;
  }
  const index = hls.levels.findIndex((level) => level.height === height);
  // currentLevel rather than nextLevel: someone who picks 480p wants 480p now,
  // and a switch the quality menu does not reflect is a menu that lies.
  if (index >= 0) hls.currentLevel = index;
}

function plyrConfig(levels, state) {
  const config = {
    // Both of these default to cdn.plyr.io: the icon sprite is fetched as the
    // player is built and `blankVideo` is loaded by every destroy(), so leaving
    // them alone puts a third party in the path of every card toggle.
    iconUrl: `${VENDOR_DIR}plyr.svg`,
    blankVideo: `${VENDOR_DIR}blank.mp4`,
    // Plyr has no HLS support of its own -- it cannot read a manifest -- so the
    // level list and the Auto label have to come from here.
    i18n: { qualityLabel: { ...state?.levelLabels, [AUTO_QUALITY]: "Auto" } },
  };

  if (levels) {
    config.quality = {
      // `forced` is Plyr's hook for a source it cannot inspect: it uses this
      // list instead of looking for <source size> elements, and routes the
      // picked value to onChange rather than trying to set a URL itself.
      forced: true,
      options: levels,
      onChange: (height) => selectLevel(state, height),
    };
  }

  return config;
}

function startPlyr(state, levels) {
  const Plyr = window.Plyr;
  // Nothing to do without the library, and nothing to do twice -- a manifest
  // can still arrive after the card was collapsed, and a player built into a
  // detached element would never be torn down by anything.
  if (typeof Plyr !== "function" || state.plyr) return;
  if (players.get(state.host) !== state) return;

  state.plyr = new Plyr(state.video, plyrConfig(levels, state));
  // Without this the settings menu reads "Quality: undefined": Plyr asks the
  // media element which level it is on, and an hls.js element has no answer.
  if (levels) state.plyr.quality = AUTO_QUALITY;
}

// ---------------------------------------------------------------------------
// The editing panel. One shared view, opened from a finished job on the upload
// card or from any Library entry. It builds the body sprint3-plan.md §1
// specifies:
//
//   POST /jobs/{id}/edit   { "operations": [ { "operation": ..., "params": {...} } ] }
//     -> 202 { job_id }
//
// The array's order is not execution order -- the worker runs clip, crop,
// scale, convert whatever order it is handed -- so the panel sends them in the
// order the sections are stacked, which is the order a person reads them.
//
// Crop and clip are Track B's and Track C's components; this panel mounts them
// and owns nothing else about them. EDITOR-COMPONENTS.md is the contract.
// ---------------------------------------------------------------------------

/** The job being edited. Null unless the editor is open. */
let editingJob = null;
/** What the mounted components last reported, or null until the user has made
 *  a selection -- which is what keeps Process disabled for a section that is
 *  ticked but not yet set up. */
let cropSelection = null;
let clipSelection = null;
/** Cleanup functions for the components mounted in this editing session. */
let mountedComponents = [];

/** Operations added since sprint 6 plug in with markup alone (sprint 6 plan,
 *  2.1): a section in the edit form like
 *
 *    <details class="edit-op" data-operation="rotate" data-mount="mountRotatePanel">
 *      <summary><input type="checkbox" /> <span class="edit-op-name">Rotate</span></summary>
 *      <div class="edit-op-body"><div class="edit-mount"></div></div>
 *    </details>
 *
 *  plus a script defining `window.mountRotatePanel(video, onChange, frame)`.
 *  Whatever the component reports is sent as that operation's params, as-is:
 *  the schema in app/schemas/edit.py is the contract for its shape. This file
 *  is not edited to add one. */
const pluginOperations = [
  ...el.editForm.querySelectorAll("details[data-operation][data-mount]"),
].map((section) => ({
  name: section.dataset.operation,
  mountName: section.dataset.mount,
  checkbox: section.querySelector("summary input[type=checkbox]"),
  host: section.querySelector(".edit-mount"),
}));
/** What each plugged-in component last reported, by operation name. */
const pluginSelections = new Map();

function setEditStatus(message) {
  el.editStatus.textContent = message;
  el.editStatus.hidden = !message;
}

/** The API answers a validation failure with FastAPI's detail list and most
 *  other things with { error }. Both have to reach the user as one line. */
function apiErrorMessage(data, status) {
  if (typeof data?.error === "string" && data.error) return data.error;
  if (typeof data?.detail === "string" && data.detail) return `${data.detail} (HTTP ${status})`;
  if (Array.isArray(data?.detail) && data.detail.length) {
    const parts = data.detail.map((item) => item.msg ?? JSON.stringify(item));
    return `${parts.join("; ")} (HTTP ${status})`;
  }
  return `HTTP ${status}`;
}

/** The request body, or an empty array when nothing is configured. */
function editOperations() {
  const operations = [];

  if (el.editClip.checked && clipSelection) {
    operations.push({
      operation: "clip",
      params: { start: clipSelection.start, end: clipSelection.end },
    });
  }
  if (el.editCrop.checked && cropSelection) {
    operations.push({
      operation: "crop",
      params: {
        x: cropSelection.x,
        y: cropSelection.y,
        w: cropSelection.w,
        h: cropSelection.h,
      },
    });
  }
  // Downscale and upscale are one slot, not two. The checkboxes are kept
  // mutually exclusive, and this is the second lock on the same door: the
  // combination the worker answers with a 422 cannot be built even if the
  // first lock is ever broken.
  if (el.editDownscale.checked) {
    operations.push({
      operation: "downscale",
      params: { height: Number(el.editDownscaleHeight.value) },
    });
  } else if (el.editUpscale.checked) {
    operations.push({
      operation: "upscale",
      params: { height: Number(el.editUpscaleHeight.value) },
    });
  }
  for (const plugin of pluginOperations) {
    const params = pluginSelections.get(plugin.name);
    if (plugin.checkbox?.checked && params) operations.push({ operation: plugin.name, params });
  }
  if (el.editConvert.checked) {
    operations.push({
      operation: "convert",
      params: { format: el.editConvertFormat.value },
    });
  }

  return operations;
}

/** Process is offered exactly when there is something to send, and the line
 *  under it says which of the two reasons it is. */
function updateProcessButton() {
  const count = editOperations().length;
  el.editProcess.disabled = count === 0;
  el.editHint.textContent = count
    ? `${count} operation${count === 1 ? "" : "s"} in one pass.`
    : "Check an operation and set it up first — a box to crop, a range to keep, or one of the dropdowns.";
}

// What the panel offers when the API has not said what this video may become:
// every job from before sprint 4, and any job whose dimensions have not been
// stored yet. `edit_options` replaces these per video when it is there.
const DEFAULT_SCALE_HEIGHTS = {
  downscale: [...el.editDownscaleHeight.options].map((option) => Number(option.value)),
  upscale: [...el.editUpscaleHeight.options].map((option) => Number(option.value)),
};

/** The API's `edit_options` for the job the editor is open on, or null when
 *  the API did not send any. See `applyScaleOptions`. */
let scaleOptions = null;

/** The heights `edit_options` allows for `operation`, or null when the API has
 *  not said -- in which case the panel's own list stands. */
function scaleHeights(operation) {
  const offered = scaleOptions?.[operation];
  return Array.isArray(offered) ? offered : null;
}

/** Point the two scale sections at what this video may actually become.
 *
 * `edit_options` is computed by the same rules `POST /jobs/{id}/edit`
 * enforces, so the dropdowns offer exactly what the API accepts and the panel
 * cannot build a request that comes back a 422.
 *
 * An empty list means the operation cannot apply at all -- a 2160p source has
 * nothing above it to upscale to -- so that section is hidden rather than left
 * disabled: no future state of this job makes it usable.
 *
 * Absent means the job is not done, or has no stored dimensions, and the
 * panel keeps its own fixed lists. The worker checks either way.
 */
function applyScaleOptions(job) {
  scaleOptions = job.edit_options ?? null;

  const sections = [
    { name: "downscale", section: el.editDownscaleSection, select: el.editDownscaleHeight },
    { name: "upscale", section: el.editUpscaleSection, select: el.editUpscaleHeight },
  ];

  for (const { name, section, select } of sections) {
    const heights = scaleHeights(name) ?? DEFAULT_SCALE_HEIGHTS[name];
    section.hidden = heights.length === 0;
    select.replaceChildren(
      ...heights.map((height) => {
        const option = document.createElement("option");
        option.value = String(height);
        option.textContent = `${height}p`;
        return option;
      }),
    );
  }
}

/** Downscale and upscale are mutually exclusive, so ticking one closes the
 *  other rather than letting the panel build a request the worker refuses. An
 *  operation this video has no use for stays disabled. */
function setScaleMode(mode) {
  el.editDownscale.disabled = mode === "upscale" || scaleHeights("downscale")?.length === 0;
  el.editUpscale.disabled = mode === "downscale" || scaleHeights("upscale")?.length === 0;
  if (mode === "downscale") el.editUpscale.checked = false;
  if (mode === "upscale") el.editDownscale.checked = false;
  updateProcessButton();
}

function showComponentNote(host, message) {
  const note = document.createElement("p");
  note.className = "hint";
  note.textContent = message;
  host.replaceChildren(note);
}

function unmountEditComponents() {
  for (const cleanup of mountedComponents) cleanup();
  mountedComponents = [];
}

/** Hand the video to Track B's crop box and Track C's scrubber, when they are
 *  on the page. A component that has not landed yet leaves its section
 *  disabled with a note, rather than offering an operation with no parameters
 *  for the worker to refuse.
 *
 * The element is the one the panel just mounted, so a component can read the
 * video's real dimensions off it; the callback is the only thing that comes
 * back. See EDITOR-COMPONENTS.md. */
function mountEditComponents(frame = null) {
  unmountEditComponents();
  cropSelection = null;
  clipSelection = null;
  pluginSelections.clear();

  const video = el.editPlayer.querySelector("video");
  const components = [
    {
      checkbox: el.editCrop,
      host: el.editCropMount,
      mount: window.mountCropBox,
      // The original's real size: the rendition playing can be smaller.
      options: frame?.width && frame?.height ? { width: frame.width, height: frame.height } : undefined,
      missing: "Coming in the next update.",
      apply: (selection) => {
        cropSelection = selection;
      },
    },
    {
      checkbox: el.editClip,
      host: el.editClipMount,
      mount: window.mountClipScrubber,
      missing: "Coming in the next update.",
      apply: (selection) => {
        clipSelection = selection;
      },
    },
    ...pluginOperations.map((plugin) => ({
      checkbox: plugin.checkbox,
      host: plugin.host,
      mount: window[plugin.mountName],
      // The original's size and length, so a panel can draw a preview to
      // scale without waiting for the element's own metadata.
      options: {
        width: frame?.width ?? null,
        height: frame?.height ?? null,
        duration: frame?.duration_seconds ?? null,
      },
      missing: "Coming in the next update.",
      apply: (selection) => {
        pluginSelections.set(plugin.name, selection);
      },
    })),
  ];

  for (const component of components) {
    if (!video || typeof component.mount !== "function") {
      component.checkbox.checked = false;
      component.checkbox.disabled = true;
      showComponentNote(component.host, component.missing);
      continue;
    }

    component.checkbox.disabled = false;
    component.host.replaceChildren();
    const report = (selection) => {
      component.apply(selection);
      updateProcessButton();
    };
    const cleanup = component.options
      ? component.mount(video, report, component.options)
      : component.mount(video, report);
    if (typeof cleanup === "function") mountedComponents.push(cleanup);
  }
}

/** Bumped by every open, so an original that arrives after the reader has
 *  moved on to another video is dropped rather than mounted. */
let editorOpens = 0;

/** The job whose video an edit of `job` starts from. Since sprint 5 that is
 *  the original upload (`edit_source`), so crop is drawn in its pixels and
 *  clip on its timeline whichever edit was clicked. Resolves to `job` itself
 *  when it is the original, or the API did not say. */
async function editSourceJob(job) {
  const sourceId = job.edit_source?.job_id;
  if (!sourceId || sourceId === job.id) return job;
  const res = await fetch(`${API}/jobs/${sourceId}`);
  if (res.status === 401) {
    showSignedOut();
    setAuthStatus("Your session expired. Sign in again.");
    return null;
  }
  if (!res.ok) throw new Error(`HTTP ${res.status}`);
  return res.json();
}

function showEditSource(job, source) {
  el.editSource.textContent =
    source.id === job.id ? job.filename : `${source.filename} — the original of ${job.filename}`;
}

function openEditor(job) {
  if (!job || job.status !== "done") return;

  editingJob = job;
  const open = ++editorOpens;
  el.editSource.textContent = job.filename;
  el.editResult.hidden = true;
  el.editProgress.hidden = true;
  setEditStatus("");

  for (const box of editCheckboxes()) {
    box.checked = false;
  }
  applyScaleOptions(job);
  setScaleMode(null);

  switchView("edit");
  const sourceId = job.edit_source?.job_id;
  if (!sourceId || sourceId === job.id) {
    mountEditor(job, job);
    return;
  }

  // An edit: play its original instead. Until it arrives, crop and clip have
  // nothing honest to measure against, so they wait rather than mount on the
  // edit's own, smaller or shorter, video.
  unmountPlayer(el.editPlayer);
  mountEditComponents();
  setEditStatus("Loading the original upload…");
  editSourceJob(job)
    .then((source) => {
      if (open !== editorOpens || editingJob !== job || !source) return;
      setEditStatus("");
      mountEditor(job, source);
    })
    .catch((err) => {
      if (open !== editorOpens || editingJob !== job) return;
      setEditStatus(`Could not load the original upload: ${err.message}`);
    });
}

function mountEditor(job, source) {
  showEditSource(job, source);
  mountPlayer(el.editPlayer, source);
  mountEditComponents(job.edit_source);
  updateProcessButton();
}

/** Leaving the editor ends the session. The components' listeners and the
 *  players both outlive their usefulness otherwise, and an edit still being
 *  followed is stopped -- the result is a library entry, not a draft to come
 *  back to. */
function closeEditor() {
  unmountEditComponents();
  stopPolling("edit");
  unmountPlayer(el.editPlayer);
  unmountPlayer(el.editResultPlayer);
  editingJob = null;
}

function showEditResult(job) {
  el.editProgress.hidden = true;
  setEditStatus("Done — the result is a new library entry.");
  el.editResult.hidden = false;
  el.editResultName.textContent = job.filename;
  el.editResultDownload.href = job.output_url ?? "#";
  el.editResultDownload.setAttribute("download", job.filename);

  // A remux or an audio extraction can come back in a container the browser
  // will not play inline. Offering the download is the honest answer; saying
  // so beats a black rectangle with no explanation.
  const mayNotPlayInline = /\.(mkv|mp3)$/i.test(job.filename);
  el.editResultNote.hidden = !mayNotPlayInline;
  el.editResultNote.textContent = mayNotPlayInline
    ? "Your browser may not play this inline — use Download if it does not start."
    : "";

  mountPlayer(el.editResultPlayer, job);
  updateProcessButton();
  // Same as switching to Library: the new job is an ordinary entry now, so
  // make sure it is there when the user goes back.
  void loadLibrary();
}

el.editBack.addEventListener("click", () => switchView("library"));
el.jobEdit.addEventListener("click", () => {
  if (activeJobDoc) openEditor(activeJobDoc);
});

/** Every operation's checkbox: the panel's own five and any plugged in. */
function editCheckboxes() {
  return [
    el.editCrop,
    el.editClip,
    el.editDownscale,
    el.editUpscale,
    el.editConvert,
    ...pluginOperations.map((plugin) => plugin.checkbox).filter(Boolean),
  ];
}

for (const box of editCheckboxes()) {
  // The checkbox lives inside <summary>, which is what makes the section
  // keyboard-reachable. Stopping the click here keeps ticking a box from also
  // opening or closing its section in browsers that treat the whole summary
  // as one target.
  box.addEventListener("click", (event) => event.stopPropagation());
  box.addEventListener("change", () => {
    if (box !== el.editDownscale && box !== el.editUpscale) {
      updateProcessButton();
      return;
    }
    // Ticking one closes the other; unticking either frees them both.
    const mode = box.checked ? (box === el.editDownscale ? "downscale" : "upscale") : null;
    setScaleMode(mode);
  });
}

el.editForm.addEventListener("submit", async (event) => {
  event.preventDefault();
  const operations = editOperations();
  if (!editingJob || operations.length === 0) return;

  el.editProcess.disabled = true;
  el.editResult.hidden = true;
  setEditStatus("Sending the edit...");

  try {
    const res = await fetch(`${API}/jobs/${editingJob.id}/edit`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ operations }),
    });

    if (res.status === 401) {
      showSignedOut();
      setAuthStatus("Your session expired. Sign in again.");
      return;
    }

    const data = await res.json().catch(() => ({}));
    if (!res.ok || !data.job_id) {
      setEditStatus(`Could not start the edit: ${apiErrorMessage(data, res.status)}`);
      updateProcessButton();
      return;
    }

    setEditStatus(PROGRESS_LABEL.queued);
    followJob("edit", data.job_id, {
      onJob(job, attempt) {
        el.editProgress.hidden = job.status !== "processing";
        if (PROGRESS_LABEL[job.status]) setEditStatus(progressLabel(job, attempt));
      },
      onDone(job) {
        showEditResult(job);
      },
      onFailed(job) {
        el.editProgress.hidden = true;
        setEditStatus(`The edit failed: ${job.error ?? "Unknown error"}`);
        updateProcessButton();
      },
      onError(err) {
        el.editProgress.hidden = true;
        setEditStatus(`Polling failed: ${err.message}`);
        updateProcessButton();
      },
    });
  } catch (err) {
    setEditStatus(`Request failed: ${err.message}`);
    updateProcessButton();
  }
});

// ---------------------------------------------------------------------------
// Library: the caller's own jobs, paginated. GET /jobs does not return a
// total count, so paging is honest about that -- "Next" is offered only when
// a full page came back, never a fabricated "of N".
// ---------------------------------------------------------------------------

let libraryOffset = 0;
let libraryFilter = "all";
let libraryJobs = [];

// The dot's colour comes from `currentColor` on the badge's own class -- it
// does not need to know which status it is drawing.
function badgeDot() {
  const span = document.createElement("span");
  span.className = "badge-icon";
  span.setAttribute("aria-hidden", "true");
  return span;
}

function makeBadge(status) {
  const badge = document.createElement("span");
  badge.className = `badge badge-${status in STATUS_LABEL ? status : "queued"}`;
  badge.append(badgeDot(), document.createTextNode(STATUS_LABEL[status] ?? status));
  return badge;
}

/** The poster frame, when the API has extracted one for this job.
 *
 * `thumbnail_url` is a presigned URL, so it can outlive the object it points
 * at: a frame deleted with its job, or one whose signature simply expired
 * between page loads, would otherwise leave a broken-image icon sitting on
 * the card. Removing the element on `error` puts the gradient placeholder
 * back, which is exactly what a job with no frame shows anyway.
 */
function makeThumbImage(job) {
  const img = document.createElement("img");
  img.className = "thumb-image";
  img.setAttribute("src", job.thumbnail_url);
  // Decorative: the filename sits directly below, so a spoken alt would only
  // repeat the name of the video the reader is already on.
  img.setAttribute("alt", "");
  // A Library page is mostly cards nobody has scrolled to yet, and every
  // poster is a separate presigned fetch.
  img.setAttribute("loading", "lazy");
  img.setAttribute("decoding", "async");
  img.addEventListener("error", () => img.remove());
  return img;
}

function renderLibrary() {
  // Re-rendering throws the cards away, and a card can be holding a player.
  // Both libraries keep listeners on the element they were handed, so they
  // have to be destroyed before their cards go, not left to the GC.
  unmountPlayersIn(el.libraryGrid);
  el.libraryGrid.replaceChildren();
  const visible = libraryJobs.filter((j) => libraryFilter === "all" || j.status === libraryFilter);

  el.libraryEmpty.hidden = visible.length > 0;
  el.libraryGrid.hidden = visible.length === 0;

  for (const job of visible) {
    const card = document.createElement("div");
    card.className = "video-card";

    const thumb = document.createElement("div");
    thumb.className = "video-thumb";
    // First, so the player that expands later -- and the badge and play
    // overlay after it -- paint on top of the frame rather than under it.
    if (job.thumbnail_url) thumb.appendChild(makeThumbImage(job));
    thumb.appendChild(makeBadge(job.status));

    // Playable means the API handed back something to play: the ladder or the
    // MP4. Every done job has the MP4, but the card should not depend on that.
    if (job.status === "done" && (job.output_url || job.hls_url)) {
      const playIcon = document.createElement("div");
      playIcon.className = "play-overlay";
      thumb.appendChild(playIcon);
      thumb.classList.add("playable");
      thumb.addEventListener("click", () => toggleInlinePlayer(thumb, job));
    }

    const body = document.createElement("div");
    body.className = "video-card-body";

    const head = document.createElement("div");
    head.className = "video-card-head";
    const name = document.createElement("div");
    name.className = "video-filename";
    name.textContent = job.filename;
    head.append(name);
    if (job.status === "done") head.append(makeEditButton(job));
    head.append(makeDeleteButton(job));
    body.appendChild(head);

    if (job.status === "failed" && job.error) {
      const err = document.createElement("div");
      err.className = "video-error";
      err.textContent = job.error;
      body.appendChild(err);
    }

    card.append(thumb, body);
    el.libraryGrid.appendChild(card);
  }

  el.libraryPrev.disabled = libraryOffset === 0;
  el.libraryNext.disabled = libraryJobs.length < PAGE_SIZE;
  el.libraryPageLabel.textContent = `Page ${Math.floor(libraryOffset / PAGE_SIZE) + 1}`;
}

const SVG_NS = "http://www.w3.org/2000/svg";
const TRASH_ICON_PATHS = [
  "M4 7h16",
  "M10 11v6",
  "M14 11v6",
  "M6 7l1 12a2 2 0 0 0 2 2h6a2 2 0 0 0 2-2l1-12",
  "M9 7V4a1 1 0 0 1 1-1h4a1 1 0 0 1 1 1v3",
];

function trashIcon() {
  const svg = document.createElementNS(SVG_NS, "svg");
  svg.setAttribute("width", "14");
  svg.setAttribute("height", "14");
  svg.setAttribute("viewBox", "0 0 24 24");
  svg.setAttribute("fill", "none");
  svg.setAttribute("stroke", "currentColor");
  svg.setAttribute("stroke-width", "1.75");
  svg.setAttribute("stroke-linecap", "round");
  svg.setAttribute("stroke-linejoin", "round");
  svg.setAttribute("aria-hidden", "true");
  for (const d of TRASH_ICON_PATHS) {
    const path = document.createElementNS(SVG_NS, "path");
    path.setAttribute("d", d);
    svg.appendChild(path);
  }
  return svg;
}

function makeDeleteButton(job) {
  const button = document.createElement("button");
  button.type = "button";
  button.className = "card-delete";
  button.title = "Delete this video";
  button.setAttribute("aria-label", `Delete ${job.filename}`);
  button.appendChild(trashIcon());
  // Clicking the card also opens the inline player for a done video -- the
  // delete button sits inside that same click target, so it has to stop the
  // event or every delete would also toggle playback underneath it.
  button.addEventListener("click", (event) => {
    event.stopPropagation();
    void deleteJob(job);
  });
  return button;
}

/** Open the editor on a finished job. The result is a new library entry; the
 *  source is left exactly where it is. */
function makeEditButton(job) {
  const button = document.createElement("button");
  button.type = "button";
  button.className = "btn-ghost card-edit";
  button.textContent = "Edit";
  button.setAttribute("aria-label", `Edit ${job.filename}`);
  button.addEventListener("click", () => openEditor(job));
  return button;
}

async function deleteJob(job) {
  if (!window.confirm(`Delete "${job.filename}"? This cannot be undone.`)) return;

  try {
    const res = await fetch(`${API}/jobs/${job.id}`, { method: "DELETE" });

    if (res.status === 401) {
      showSignedOut();
      setAuthStatus("Your session expired. Sign in again.");
      return;
    }

    if (!res.ok && res.status !== 204) {
      const data = await res.json().catch(() => ({}));
      setLibraryStatus(`Could not delete "${job.filename}": ${data.error ?? res.status}`);
      return;
    }

    libraryJobs = libraryJobs.filter((j) => j.id !== job.id);
    renderLibrary();
  } catch (err) {
    setLibraryStatus(`Request failed: ${err.message}`);
  }
}

function setLibraryStatus(message) {
  el.libraryStatus.textContent = message;
  el.libraryStatus.hidden = false;
}

/** Swap a done card's thumbnail for an inline player, in place, on click.
 *
 * `container` is the Library card's thumbnail or the Admin table's preview
 * cell -- anything whose click opens the player. The player goes into a
 * .player-host of its own so that collapsing removes exactly one thing, and
 * so the badge and the play overlay next to it survive. */
function toggleInlinePlayer(container, job) {
  const host = container.querySelector(".player-host");
  if (host) {
    unmountPlayer(host);
    host.remove();
    container.classList.remove("expanded");
    return;
  }

  const playerHost = document.createElement("div");
  playerHost.className = "player-host";
  // The player sits inside the element whose click opened it, so without this
  // every press of play, pause or the settings menu would collapse the card
  // it is playing in.
  playerHost.addEventListener("click", (event) => event.stopPropagation());
  container.appendChild(playerHost);
  mountPlayer(playerHost, job, { autoplay: true });
  container.classList.add("expanded");
}

async function loadLibrary() {
  try {
    const res = await fetch(`${API}/jobs?limit=${PAGE_SIZE}&offset=${libraryOffset}`);
    if (res.status === 401) {
      showSignedOut();
      setAuthStatus("Your session expired. Sign in again.");
      return;
    }
    libraryJobs = res.ok ? await res.json() : [];
  } catch {
    libraryJobs = [];
  }
  renderLibrary();
}

for (const button of el.libraryFilters.querySelectorAll("[data-status]")) {
  button.addEventListener("click", () => {
    libraryFilter = button.dataset.status;
    for (const b of el.libraryFilters.querySelectorAll("[data-status]")) {
      b.classList.toggle("active", b === button);
    }
    renderLibrary();
  });
}
el.libraryPrev.addEventListener("click", () => {
  libraryOffset = Math.max(0, libraryOffset - PAGE_SIZE);
  void loadLibrary();
});
el.libraryNext.addEventListener("click", () => {
  libraryOffset += PAGE_SIZE;
  void loadLibrary();
});

// ---------------------------------------------------------------------------
// Admin: every job, operator only. The response carries the same job shape as
// GET /jobs -- no owner and no timestamps -- so this table shows only what
// the API actually returns rather than inventing columns it cannot fill.
// ---------------------------------------------------------------------------

let adminOffset = 0;
let adminFilter = "all";
let adminJobs = [];

function renderAdmin() {
  unmountPlayersIn(el.adminTbody);
  el.adminTbody.replaceChildren();
  const visible = adminJobs.filter((j) => adminFilter === "all" || j.status === adminFilter);

  el.adminEmpty.hidden = visible.length > 0;
  el.adminTbody.parentElement.hidden = visible.length === 0;

  for (const job of visible) {
    const row = document.createElement("tr");

    const filenameCell = document.createElement("td");
    filenameCell.textContent = job.filename;

    const statusCell = document.createElement("td");
    statusCell.appendChild(makeBadge(job.status));

    // A plain link here would navigate to the presigned URL, which the
    // object store deliberately serves as a forced download (see
    // app/services/output_urls.py) -- exactly what made "checking which
    // video is which" mean downloading every one of them. A <video> element
    // ignores that header by design, the same way the Library grid's
    // click-to-play already does, so this plays the file in place instead.
    const previewCell = document.createElement("td");
    if (job.status === "done" && (job.output_url || job.hls_url)) {
      const watchButton = document.createElement("button");
      watchButton.type = "button";
      watchButton.className = "btn-ghost admin-watch";
      watchButton.textContent = "Watch";
      watchButton.addEventListener("click", () => {
        toggleInlinePlayer(previewCell, job);
        const playing = previewCell.querySelector(".player-host") !== null;
        watchButton.textContent = playing ? "Hide" : "Watch";
      });
      previewCell.appendChild(watchButton);
    } else {
      previewCell.textContent = "—";
    }

    const actionsCell = document.createElement("td");
    actionsCell.appendChild(makeAdminDeleteButton(job));

    row.append(filenameCell, statusCell, previewCell, actionsCell);
    el.adminTbody.appendChild(row);
  }

  el.adminPrev.disabled = adminOffset === 0;
  el.adminNext.disabled = adminJobs.length < PAGE_SIZE;
  el.adminPageLabel.textContent = `Page ${Math.floor(adminOffset / PAGE_SIZE) + 1}`;
}

function makeAdminDeleteButton(job) {
  const button = document.createElement("button");
  button.type = "button";
  button.className = "card-delete";
  button.title = "Delete this video";
  button.setAttribute("aria-label", `Delete ${job.filename}`);
  button.appendChild(trashIcon());
  button.addEventListener("click", () => deleteJobAsAdmin(job));
  return button;
}

async function deleteJobAsAdmin(job) {
  if (!window.confirm(`Delete "${job.filename}"? This cannot be undone.`)) return;

  try {
    // Unscoped: an operator can delete any user's job, not just their own --
    // the same "identify what this upload is" story the inline preview
    // above exists for.
    const res = await fetch(`${API}/admin/jobs/${job.id}`, { method: "DELETE" });

    if (res.status === 401) {
      showSignedOut();
      setAuthStatus("Your session expired. Sign in again.");
      return;
    }
    if (res.status === 403) {
      switchView("upload");
      return;
    }
    if (!res.ok && res.status !== 204) {
      const data = await res.json().catch(() => ({}));
      setAdminStatus(`Could not delete "${job.filename}": ${data.error ?? res.status}`);
      return;
    }

    adminJobs = adminJobs.filter((j) => j.id !== job.id);
    renderAdmin();
  } catch (err) {
    setAdminStatus(`Request failed: ${err.message}`);
  }
}

function setAdminStatus(message) {
  el.adminStatus.textContent = message;
  el.adminStatus.hidden = false;
}

async function loadAdmin() {
  try {
    const res = await fetch(`${API}/admin/jobs?limit=${PAGE_SIZE}&offset=${adminOffset}`);
    if (res.status === 401) {
      showSignedOut();
      setAuthStatus("Your session expired. Sign in again.");
      return;
    }
    if (res.status === 403) {
      switchView("upload");
      return;
    }
    adminJobs = res.ok ? await res.json() : [];
  } catch {
    adminJobs = [];
  }
  renderAdmin();
}

for (const button of el.adminFilters.querySelectorAll("[data-status]")) {
  button.addEventListener("click", () => {
    adminFilter = button.dataset.status;
    for (const b of el.adminFilters.querySelectorAll("[data-status]")) {
      b.classList.toggle("active", b === button);
    }
    renderAdmin();
  });
}
el.adminPrev.addEventListener("click", () => {
  adminOffset = Math.max(0, adminOffset - PAGE_SIZE);
  void loadAdmin();
});
el.adminNext.addEventListener("click", () => {
  adminOffset += PAGE_SIZE;
  void loadAdmin();
});

// ---------------------------------------------------------------------------
// Admin: accounts. GET /admin/users lists them with how many jobs each owns;
// DELETE /admin/users/{id} removes one with every file it stored (contract
// §2.4). An operator's own row has no Delete -- the API refuses it with 409.
// ---------------------------------------------------------------------------

/** One page is every account a course project will have; past it, say so
 *  rather than paging a list nobody needs paged yet. */
const ADMIN_USERS_LIMIT = 200;
let adminUsers = [];

function setAdminUsersStatus(message) {
  el.adminUsersStatus.textContent = message;
  el.adminUsersStatus.hidden = !message;
}

function renderAdminUsers() {
  el.adminUsersTbody.replaceChildren();
  for (const user of adminUsers) {
    const row = document.createElement("tr");
    const email = document.createElement("td");
    email.textContent = user.email;
    const role = document.createElement("td");
    role.textContent = user.role;
    const count = document.createElement("td");
    count.textContent = String(user.job_count);
    const actions = document.createElement("td");
    if (user.id !== currentUser?.id) {
      const button = document.createElement("button");
      button.type = "button";
      button.className = "card-delete";
      button.title = "Delete this account";
      button.setAttribute("aria-label", `Delete account ${user.email}`);
      button.appendChild(trashIcon());
      button.addEventListener("click", () => deleteAccount(user));
      actions.appendChild(button);
    }
    row.append(email, role, count, actions);
    el.adminUsersTbody.appendChild(row);
  }
  el.adminUsersMore.hidden = adminUsers.length < ADMIN_USERS_LIMIT;
  el.adminUsersMore.textContent = `Showing the first ${ADMIN_USERS_LIMIT} accounts.`;
}

async function loadAdminUsers() {
  try {
    const res = await fetch(`${API}/admin/users?limit=${ADMIN_USERS_LIMIT}`);
    // 401 and 403 are the jobs table's to act on; it asks at the same moment.
    adminUsers = res.ok ? await res.json() : [];
  } catch {
    adminUsers = [];
  }
  renderAdminUsers();
}

function describeVideos(count) {
  return count === 1 ? "1 video" : `${count} videos`;
}

async function deleteAccount(user) {
  const question =
    `Delete the account ${user.email} and its ${describeVideos(user.job_count)}? ` +
    "This cannot be undone.";
  if (!window.confirm(question)) return;

  try {
    const res = await fetch(`${API}/admin/users/${user.id}`, { method: "DELETE" });
    if (res.status === 401) {
      showSignedOut();
      setAuthStatus("Your session expired. Sign in again.");
      return;
    }
    if (res.status === 403) {
      switchView("upload");
      return;
    }
    if (!res.ok && res.status !== 404) {
      const data = await res.json().catch(() => ({}));
      setAdminUsersStatus(`Could not delete ${user.email}: ${data.error ?? res.status}`);
      return;
    }
    // 404: someone else deleted it first. Either way it is gone.
    setAdminUsersStatus(`Deleted ${user.email}.`);
    adminUsers = adminUsers.filter((u) => u.id !== user.id);
    renderAdminUsers();
    // Its jobs went with it.
    void loadAdmin();
  } catch (err) {
    setAdminUsersStatus(`Request failed: ${err.message}`);
  }
}

// ---------------------------------------------------------------------------

applyAuthMode();
// The editor's button state is derived from its inputs rather than written
// into the markup twice; this is the initial pass.
updateProcessButton();
// Decide which half of the page to show before anything else runs.
void refreshIdentity();
// Public, so it is read whether or not there is a session -- the limit belongs
// next to the picker, and the picker is behind the sign-in form.
loadLimits().then(applyLimitHint);
