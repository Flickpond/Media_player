// Flickpond frontend entry point: sign in -> upload -> poll -> play, plus a
// library of past uploads and an operator view over everyone's jobs.
//
// Backend endpoints it depends on (see docs/contract.md):
//   POST /auth/register  -> 201 { id, email, role }, sets the session cookie
//   POST /auth/login     -> 200 { id, email, role }, sets the session cookie
//   POST /auth/logout    -> 204, clears it
//   GET  /auth/me        -> 200 { id, email, role } | 401
//   POST /upload         -> 202 { job_id }, sets no cookie
//   GET  /jobs/{id}      -> { id, filename, status, output_url?, error? }
//   GET  /jobs?limit&offset       -> [ ...job shape... ]   (caller's own jobs)
//   GET  /admin/jobs?limit&offset -> [ ...job shape... ]   (operator only)
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
  fileInput: document.getElementById("file-input"),
  browseButton: document.getElementById("browse-button"),
  status: document.getElementById("status"),
  activeJob: document.getElementById("active-job"),
  jobFilename: document.getElementById("job-filename"),
  jobBadge: document.getElementById("job-badge"),
  jobDelete: document.getElementById("job-delete"),
  jobProgress: document.getElementById("job-progress"),
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
}

function showSignedOut() {
  currentUser = null;
  el.auth.hidden = false;
  el.app.hidden = true;
  stopPolling();
  el.activeJob.hidden = true;
  el.player.hidden = true;
  el.player.removeAttribute("src");
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
const VIEWS = { upload: el.viewUpload, library: el.viewLibrary, admin: el.viewAdmin };

function switchView(name) {
  if (name === "admin" && (!currentUser || currentUser.role !== "operator")) {
    name = "upload";
  }
  for (const [key, section] of Object.entries(VIEWS)) {
    section.hidden = key !== name;
  }
  for (const [key, button] of Object.entries(NAV_BUTTONS)) {
    button.classList.toggle("active", key === name);
  }
  if (name === "library") loadLibrary();
  if (name === "admin") loadAdmin();
}

el.navUpload.addEventListener("click", () => switchView("upload"));
el.navLibrary.addEventListener("click", () => switchView("library"));
el.navAdmin.addEventListener("click", () => switchView("admin"));

// ---------------------------------------------------------------------------
// Upload + the active job's status. One job is tracked at a time: starting a
// new upload always wins over whatever the previous one was doing (P4's
// escape hatch is choosing a different file, not waiting forever).
// ---------------------------------------------------------------------------

let pollTimer = null;
let lastFile = null;
let uploading = false;
let activeJobId = null;

const POLL_INTERVAL_MS = 2000;
// 30 polls is a minute. The transcode step finishes well inside that under
// normal load, so past this something is wrong -- most likely a worker that
// died holding the job, which leaves the row at `processing` with nobody left
// to write to it. Keep polling anyway: the page cannot tell "stuck" from
// "slow", and saying it failed would be a claim we cannot support. Say it is
// taking long, hand the form back, and let the user decide.
const SLOW_AFTER_POLLS = 30;

function setStatus(message) {
  el.status.textContent = message;
  el.status.hidden = false;
}

function stopPolling() {
  if (pollTimer !== null) {
    clearTimeout(pollTimer);
    pollTimer = null;
  }
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
  el.player.hidden = true;
  el.player.removeAttribute("src");
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
  if (file) handleFile(file);
});
el.fileInput.addEventListener("change", () => {
  const file = el.fileInput.files[0];
  if (file) handleFile(file);
});

el.jobRetry.addEventListener("click", () => {
  if (lastFile) handleFile(lastFile);
});
el.jobChooseDifferent.addEventListener("click", () => el.fileInput.click());
el.jobDelete.addEventListener("click", deleteActiveJob);

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

async function handleFile(file) {
  if (uploading) return; // A rapid second drop cannot double-post the first.

  if (file.type && !ALLOWED_CONTENT_TYPES.has(file.type)) {
    setStatus(`"${file.type || "unknown type"}" is not a video format we handle.`);
    return;
  }
  if (file.size > MAX_FILE_SIZE_BYTES) {
    setStatus("That file is larger than the 100MB limit.");
    return;
  }

  // A new upload always wins over whatever the previous job was doing.
  stopPolling();
  resetJobCard();
  activeJobId = null;
  lastFile = file;
  setBusy(true);
  setStatus("Uploading...");

  try {
    const body = new FormData();
    body.append("file", file);

    const res = await fetch(`${API}/upload`, { method: "POST", body });
    const data = await res.json().catch(() => ({}));

    if (res.status === 401) {
      showSignedOut();
      setAuthStatus("Your session expired. Sign in again.");
      setBusy(false);
      return;
    }

    if (!res.ok) {
      setStatus(`Upload failed: ${data.error ?? res.status}`);
      setBusy(false);
      return;
    }

    setStatus("Received, waiting for processing...");
    el.activeJob.hidden = false;
    el.jobFilename.textContent = file.name;
    setBadge("queued");
    activeJobId = data.job_id;
    setBusy(false);
    poll(data.job_id);
  } catch (err) {
    setStatus(`Request failed: ${err.message}`);
    setBusy(false);
  }
}

async function poll(jobId, attempt = 0) {
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
    setStatus(`Polling failed: ${err.message}`);
    return; // Stop on network errors to avoid a tight loop.
  }

  setBadge(job.status);

  if (job.status === "done") {
    setStatus("Processing complete.");
    el.jobProgress.hidden = true;
    el.player.src = job.output_url;
    el.player.hidden = false;
    return; // Stop polling.
  }

  if (job.status === "failed") {
    setStatus(`Processing failed: ${job.error ?? "Unknown error"}`);
    el.jobProgress.hidden = true;
    el.player.hidden = true;
    el.jobActions.hidden = false;
    return; // Stop polling.
  }

  el.jobProgress.hidden = job.status !== "processing";

  if (attempt >= SLOW_AFTER_POLLS) {
    setStatus(`Still working on it — this is taking longer than expected. Job ${jobId}`);
    el.jobActions.hidden = false; // Give the form back; a stuck job otherwise never releases it.
  } else {
    const labels = { queued: "Queued...", processing: "Processing..." };
    setStatus(labels[job.status] ?? job.status);
  }

  pollTimer = setTimeout(() => poll(jobId, attempt + 1), POLL_INTERVAL_MS);
}

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

function renderLibrary() {
  el.libraryGrid.replaceChildren();
  const visible = libraryJobs.filter((j) => libraryFilter === "all" || j.status === libraryFilter);

  el.libraryEmpty.hidden = visible.length > 0;
  el.libraryGrid.hidden = visible.length === 0;

  for (const job of visible) {
    const card = document.createElement("div");
    card.className = "video-card";

    const thumb = document.createElement("div");
    thumb.className = "video-thumb";
    thumb.appendChild(makeBadge(job.status));

    if (job.status === "done" && job.output_url) {
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
    head.append(name, makeDeleteButton(job));
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
    deleteJob(job);
  });
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

/** Swap a done card's thumbnail for an inline player, in place, on click. */
function toggleInlinePlayer(thumb, job) {
  const existing = thumb.querySelector("video");
  if (existing) {
    existing.remove();
    thumb.classList.remove("expanded");
    return;
  }
  const video = document.createElement("video");
  video.src = job.output_url;
  video.controls = true;
  video.autoplay = true;
  thumb.appendChild(video);
  thumb.classList.add("expanded");
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
  loadLibrary();
});
el.libraryNext.addEventListener("click", () => {
  libraryOffset += PAGE_SIZE;
  loadLibrary();
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
    if (job.status === "done" && job.output_url) {
      const watchButton = document.createElement("button");
      watchButton.type = "button";
      watchButton.className = "btn-ghost admin-watch";
      watchButton.textContent = "Watch";
      watchButton.addEventListener("click", () => {
        toggleInlinePlayer(previewCell, job);
        const playing = previewCell.querySelector("video") !== null;
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
  loadAdmin();
});
el.adminNext.addEventListener("click", () => {
  adminOffset += PAGE_SIZE;
  loadAdmin();
});

// ---------------------------------------------------------------------------

applyAuthMode();
// Decide which half of the page to show before anything else runs.
refreshIdentity();
