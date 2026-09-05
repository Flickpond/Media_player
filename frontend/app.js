// Flickpond frontend entry point: upload -> poll -> play / show error.
//
// Backend endpoints it depends on (see docs/contract.md):
//   POST /upload     -> 202 { "job_id": "<uuid>" }                     (track B; not implemented yet)
//   GET  /jobs/{id}  -> { id, filename, status, output_url?, error? }  (implemented)
const API = "http://127.0.0.1:8000";

const form = document.getElementById("upload-form");
const fileInput = document.getElementById("file-input");
const uploadButton = document.getElementById("upload-button");
const statusEl = document.getElementById("status");
const player = document.getElementById("player");

let pollTimer = null;

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

async function poll(jobId) {
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

  const labels = { queued: "Queued...", processing: "Processing..." };
  setStatus(labels[job.status] ?? job.status);

  // Poll every 2 seconds.
  pollTimer = setTimeout(() => poll(jobId), 2000);
}
