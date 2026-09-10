// Flickpond frontend entry point: sign in -> upload -> poll -> play.
//
// Backend endpoints it depends on (see docs/contract.md):
//   POST /auth/register  -> 201 { id, email, role }, sets the session cookie
//   POST /auth/login     -> 200 { id, email, role }, sets the session cookie
//   POST /auth/logout    -> 204, clears it
//   GET  /auth/me        -> 200 { id, email, role } | 401
//   POST /upload         -> 202 { "job_id": "<uuid>" }
//   GET  /jobs/{id}      -> { id, filename, status, output_url?, error? }
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

const form = document.getElementById("upload-form");
const fileInput = document.getElementById("file-input");
const uploadButton = document.getElementById("upload-button");
const statusEl = document.getElementById("status");
const player = document.getElementById("player");

const authSection = document.getElementById("auth");
const appSection = document.getElementById("app");
const authForm = document.getElementById("auth-form");
const authEmail = document.getElementById("auth-email");
const authPassword = document.getElementById("auth-password");
const authSubmit = document.getElementById("auth-submit");
const authToggle = document.getElementById("auth-toggle");
const authStatus = document.getElementById("auth-status");
const who = document.getElementById("who");
const logoutButton = document.getElementById("logout");

// The session cookie is HttpOnly, so this page cannot read it and cannot tell
// whether it is signed in by looking. It has to ask the server -- that is what
// /auth/me is for, and it is why signing out is a request rather than deleting
// a cookie here.
let registering = false;

function setAuthStatus(message) {
  authStatus.textContent = message;
  authStatus.hidden = false;
}

function showSignedIn(user) {
  who.textContent = user.email;
  authSection.hidden = true;
  appSection.hidden = false;
}

function showSignedOut() {
  authSection.hidden = false;
  appSection.hidden = true;
  stopPolling();
  player.hidden = true;
  player.removeAttribute("src");
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

authToggle.addEventListener("click", () => {
  registering = !registering;
  authSubmit.textContent = registering ? "Create account" : "Sign in";
  authToggle.textContent = registering ? "Sign in instead" : "Create an account instead";
  authStatus.hidden = true;
});

authForm.addEventListener("submit", async (event) => {
  event.preventDefault();
  authSubmit.disabled = true;
  const path = registering ? "/auth/register" : "/auth/login";
  try {
    const res = await fetch(`${API}${path}`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ email: authEmail.value, password: authPassword.value }),
    });
    if (res.ok) {
      authPassword.value = "";
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
    authSubmit.disabled = false;
  }
});

logoutButton.addEventListener("click", async () => {
  await fetch(`${API}/auth/logout`, { method: "POST" }).catch(() => {});
  showSignedOut();
});

let pollTimer = null;

const POLL_INTERVAL_MS = 2000;
// 30 polls is a minute. The sprint 1 copy job finishes inside a single
// interval, so past this something is wrong -- most likely a worker that died
// holding the job, which leaves the row at `processing` with nobody left to
// write to it. Keep polling anyway: the page cannot tell "stuck" from "slow",
// and saying it failed would be a claim we cannot support. Say it is taking
// long, hand the form back, and let the user decide.
const SLOW_AFTER_POLLS = 30;

function setStatus(message) {
  statusEl.textContent = message;
  statusEl.hidden = false;
}

function stopPolling() {
  if (pollTimer !== null) {
    clearTimeout(pollTimer);
    pollTimer = null;
  }
}

form.addEventListener("submit", async (event) => {
  event.preventDefault();

  const file = fileInput.files[0];
  if (!file) {
    setStatus("Please choose a video file first.");
    return;
  }

  stopPolling();
  player.hidden = true;
  player.removeAttribute("src");
  uploadButton.disabled = true;
  setStatus("Uploading...");

  try {
    const body = new FormData();
    // TODO: confirm the multipart field name with track B's POST /upload
    // (the contract leaves it open; defaulting to "file" for now).
    body.append("file", file);

    const res = await fetch(`${API}/upload`, { method: "POST", body });
    const data = await res.json().catch(() => ({}));

    if (res.status === 401) {
      showSignedOut();
      setAuthStatus("Your session expired. Sign in again.");
      uploadButton.disabled = false;
      return;
    }

    if (!res.ok) {
      setStatus(`Upload failed: ${data.error ?? res.status}`);
      uploadButton.disabled = false;
      return;
    }

    setStatus("Received, waiting for processing...");
    poll(data.job_id);
  } catch (err) {
    setStatus(`Request failed: ${err.message}`);
    uploadButton.disabled = false;
  }
});

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
    uploadButton.disabled = false;
    return; // Stop on network errors to avoid a tight loop.
  }

  if (job.status === "done") {
    setStatus("Processing complete.");
    player.src = job.output_url;
    player.hidden = false;
    uploadButton.disabled = false;
    return; // Stop polling.
  }

  if (job.status === "failed") {
    setStatus(`Processing failed: ${job.error ?? "Unknown error"}`);
    uploadButton.disabled = false;
    return; // Stop polling.
  }

  if (attempt >= SLOW_AFTER_POLLS) {
    setStatus(`Still working on it — this is taking longer than expected. Job ${jobId}`);
    // Give the form back. Without this the button stays disabled for as long
    // as the job is stuck, which is forever, and the only way out is a reload.
    uploadButton.disabled = false;
  } else {
    const labels = { queued: "Queued...", processing: "Processing..." };
    setStatus(labels[job.status] ?? job.status);
  }

  // Poll every 2 seconds.
  pollTimer = setTimeout(() => poll(jobId, attempt + 1), POLL_INTERVAL_MS);
}

// Decide which half of the page to show before anything else runs.
refreshIdentity();
