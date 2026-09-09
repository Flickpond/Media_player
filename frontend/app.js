// Flickpond frontend entry point: upload -> poll -> play / show error.
//
// Backend endpoints it depends on (see docs/contract.md):
//   POST /upload     -> 202 { "job_id": "<uuid>" }                     (track B; not implemented yet)
//   GET  /jobs/{id}  -> { id, filename, status, output_url?, error? }  (implemented)
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
