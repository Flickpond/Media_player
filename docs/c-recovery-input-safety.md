# Sprint 3 track C: recovery and input safety

Owner: Yang Dongwei (`@ttydw-ch`). Branch: `C-status-endpoints-db`.
Baseline: `main` at `f6667c0`, fetched on 21 September 2026.

Scope follows the supplied Sprint 3 artifact screenshots: C owns retry and
source-aware validation; B owns the edit endpoint/processor, A owns HLS, and
E owns the entire UI. The older `sprint3-plan-team` branch assigning C the
clip scrubber does **not** describe this implementation.

## Files and responsibilities

```text
app/
  api/jobs.py                         POST /jobs/{id}/retry
  repositories/jobs.py                prepare_retry + worker claim locking
  repositories/__init__.py             repository export
  worker/validation.py                validate_crop / validate_clip
tests/
  test_job_retry.py                    HTTP, authentication and failure handling
  test_edit_validation.py              geometry, numeric safety and boundary cases
  integration/
    test_job_retry_postgres.py         real SQL races, rollback and worker handoff
    test_edit_validation_source.py     ffprobe on an actual portrait MP4
docs/
  contract.md                         shared API and state-transition additions
  c-recovery-input-safety.md           this handoff
```

There is no new migration. A's existing `operations` and `hls_key` columns
are used. No frontend, Compose, queue, probe, HLS or edit processor files are
changed. `app/api/jobs.py` is shared with B; preserve both route additions
when merging B's `/edit` implementation.

## Retry: how to use it

The caller must be signed in. Direct API route: `POST /jobs/{id}/retry`;
through nginx or the deployed page: `POST /api/jobs/{id}/retry`. No body.

From the same-origin browser page, with an existing login cookie:

```javascript
const response = await fetch(`/api/jobs/${jobId}/retry`, { method: "POST" });
const result = await response.json();
// On 202, poll /api/jobs/{result.job_id}, just like after an upload.
// On 409 or 503, refresh the current job before deciding whether to retry.
```

| HTTP | Meaning |
| --- | --- |
| 202 | Accepted. `{"job_id":"<same id>"}`; the worker may already have started. |
| 401 | No valid login. Use the existing sign-in flow. |
| 404 | No such job, or it belongs to someone else. Operators do not bypass ownership here. |
| 409 | The job is queued, processing or done; only failed jobs can be retried. |
| 422 | The path ID is not a UUID. |
| 503 | Retry could not be confirmed. Refresh the job and try again as appropriate. |

Retry changes the existing row, never creates a new job or uploads another
copy of the source. It retains `id`, `owner_id`, `filename`, `source_key`,
`operations` and `created_at`; clears `error`, `output_key` and `hls_key`;
sets `status=queued` and updates `updated_at`. Therefore retrying invalid
editing parameters will fail validation again: users must submit corrected
parameters as a new edit through B's endpoint.

### Why the transaction and row lock matter

`prepare_retry` runs one conditional `UPDATE`, scoped to both owner and
`status='failed'`, and deliberately does not commit. The API publishes the
same job ID through the existing `enqueue_job` seam, then commits.

Two concurrent retries of the same failed state cannot both enqueue. If
queue publication raises, rollback restores the old error and failed state,
so the next retry is possible. Other rows are not locked.

A worker can arrive before the commit. A plain conditional UPDATE can see
the old `failed` snapshot and skip it without waiting. `mark_processing`
therefore takes `SELECT ... FOR UPDATE` by ID first, waits for the retry,
then checks the state. This lock is released before video processing.

Redis and PostgreSQL still do not form a distributed transaction. A lost
response after Redis accepted a message can leave an extra delivery; if the
database rolled back, the worker cannot claim that failed row. If a commit
response is ambiguous, refresh the state after 503. This design does not
claim exactly-once execution across service crashes, nor cancel an old
worker already running when a reaper declares its lease expired.

## B: connect validation before FFmpeg

Use the existing `SourceProbe` from `app/worker/probe.py`. Probe the actual
file used as this edit's input, once, and check **every requested crop/clip
before starting FFmpeg**. Do not use browser-supplied width or duration.

```python
from app.worker.probe import probe_source
from app.worker.storage import ObjectStoreError
from app.worker.validation import validate_clip, validate_crop

probe = probe_source(downloaded_source_path)
validators = {"crop": validate_crop, "clip": validate_clip}
for item in operations:
    validate = validators.get(item["operation"])
    if validate is not None:
        try:
            validate(item["params"], probe)
        except ValueError as exc:
            raise ObjectStoreError(
                "edit validation failed", user_message=str(exc)
            ) from exc

# Only now construct and run the fixed clip -> crop -> scale -> convert command.
```

The adapter is essential: the existing worker exposes only
`ObjectStoreError.user_message`. An unwrapped `ValueError` becomes the
generic processing-failed message. Keep the catch around these validators
only; do not expose arbitrary exception strings or FFmpeg stderr.

Crop requires exactly `x`, `y`, `w`, `h`: integers (not booleans or numeric
strings), `x,y >= 0`, `w,h > 0`, `x+w <= probe.width`, `y+h <= probe.height`.
Clip requires exactly `start`, `end`: finite numeric seconds (not booleans,
strings, NaN or infinity), `0 <= start < end <= probe.duration_seconds`.
Exact frame/duration edges are valid. Parameters are never coerced,
clamped or mutated. Unusable probe dimensions/duration are rejected.

B retains ownership of API schema validation, supported operations,
duplicate operations, downscale/upscale exclusion and FFmpeg construction.
Invalid request shapes may return 422 there. Real-file bounds errors happen
in the worker and are returned by polling as `status=failed` plus `error`.

## E: display errors and retry

Show Retry only for a failed job; disable it while its request is pending.
On 202, clear the old player/error display and poll the returned ID. On 409,
refresh the job (another tab may already have retried). On 503, refresh and
show the returned readable message; do not assume the retry was accepted.

Display the worker's `error` as text, using the current failed-job UI. Proposed
wording is implemented and ready for E's review; agreement with E has **not**
been obtained by this change.

| Condition | Message |
| --- | --- |
| Crop outside the real frame | `crop must fit within the source video's frame` |
| Negative origin | `crop x and y must be zero or greater` |
| Zero/negative dimensions | `crop width and height must be greater than zero` |
| Non-integer crop | `crop coordinates and dimensions must be whole numbers` |
| Empty/reversed time range | `clip end must be greater than start` |
| Negative start | `clip start must be zero or greater` |
| End beyond the real duration | `clip end must not exceed the source video's duration` |
| Invalid/nonfinite time | `clip start and end must be finite numbers` |

## Run the tests (WSL, Python 3.12)

From `/mnt/e/workspace/Media_player`, in a development virtual environment:

```bash
python -m pip install -e '.[dev]'
python -m ruff check .
python -m pytest tests/test_job_retry.py tests/test_edit_validation.py -q
python -m pytest -q --cov=app
```

For integration tests, point `POSTGRES_DSN` at a **test PostgreSQL database**,
configure the test MinIO endpoint for the full integration suite, and have
`ffmpeg` / `ffprobe` installed (the project Docker image contains both):

```bash
python -m alembic upgrade head
RUN_POSTGRES_TESTS=1 python -m pytest \
  tests/integration/test_job_retry_postgres.py \
  tests/integration/test_edit_validation_source.py -q
RUN_POSTGRES_TESTS=1 python -m pytest tests/integration -q
```

The tests create per-test owners and remove only their rows. RQ dispatch uses
the real RQ worker with in-memory Redis; SQL concurrency uses real PostgreSQL.
The probe test generates an actual portrait MP4 and invokes real `ffprobe`.
The bad-edit path tests use a clearly labelled test adapter in place of B's
not-yet-merged processor, then the real worker state machine and status API.

### Verification recorded on 21 September 2026

Verified in WSL with an isolated Python 3.12.14 Docker test runner, PostgreSQL
16, MinIO and the project's packaged FFmpeg. Existing application volumes
were not used for testing.

- `ruff check .`: passed.
- `pytest -q --cov=app`: **286 passed**, 43 integration tests skipped as
  intended; **85.77%** application coverage, above the 80% CI gate.
- `RUN_POSTGRES_TESTS=1 pytest tests/integration -q`: **43 passed**.
- C's new tests account for **93 unit cases and 12 integration cases**.
- Same-ID retry reached `done` through a real RQ worker and real PostgreSQL,
  with in-memory Redis and a controlled processing step.
- A real generated portrait video was checked through FFmpeg/ffprobe.

These results cover the code and integration contracts. They do not claim
that B's edit processor or E's Retry UI was present, or that a deployment
verification or wording review with E has taken place.

## Integration acceptance still to perform with B and E

- B imports both validators and wraps their ValueError as shown above.
- E confirms the error wording and connects the Retry button.
- On the integrated application, a crop outside the actual frame reaches
  `failed` with the readable message and FFmpeg is not launched for that edit.
- A failed job can be retried from the browser and reaches `done` after a
  recoverable cause is removed; its original job ID and source are retained.
- After the team merges and deploys, repeat these checks on the deployed
  application and save browser evidence. Local contract tests alone do not
  establish that the complete editing UI is shipped.
