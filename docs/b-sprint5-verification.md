# Sprint 5 Part B — implementation and verification

Date: 17 October 2026. Branch: `feat/sprint5-part-b`, based on `main` at `735da33`.

## Delivered

- Each user can reserve five unfinished upload sessions. A user row lock serializes
  concurrent reservations across API replicas; open, completing and aborting sessions
  count until storage cleanup succeeds, including expired sessions. A sixth start
  returns 429 before initiating storage. No migration or new environment setting.
- `GET /admin/users?limit=50&offset=0` is operator-only and returns
  `{id, email, role, job_count}`. Pagination is bounded to 200 and stable.
- `DELETE /admin/users/{id}` returns 204, 403 for non-operators, 404 for a missing
  account, and 409 when deleting oneself, using the exact Sprint 5 error messages.
  It locks the account, upload sessions and jobs, aborts unfinished multipart
  uploads, and cleans source, output, thumbnail and every HLS object through the
  same storage cleanup helper used by individual job deletion. Borrowed edit
  sources are preserved by that helper. Only after cleanup succeeds are upload
  sessions, jobs and user rows deleted, with one database commit. Failures return
  a safe 503, roll back the transaction and can be retried. Deleted-user cookies
  receive 401 on the next authenticated request.
- Every browser file, including an empty or generic MIME type, uses direct
  uploads. Actual bytes and sizes are checked at completion; invalid content is
  removed without a job. Stored metadata remains octet-stream and attachment.
- The old `POST /upload` returns 410 with migration instructions, without a File
  dependency that would spool the request. Frontend fallback and its separate
  100 MiB limit are removed. The load fixture uses direct multipart upload too.

## Integration handoff

The backend user list supplies the account name and video count for E's delete
confirmation. E still owns the admin panel button; this branch does not add it.
The backend and frontend upload migration must deploy together.

Files shared with other tracks and changed as necessary for Part B:
`app/api/jobs.py` (extract cleanup only; existing best-effort deletion retained),
`app/repositories/users.py`, `app/schemas/user.py`, `app/schemas/upload.py`,
`app/main.py`, `frontend/app.js` and its tests, plus the load fixture and docs.
No worker behavior, infrastructure, live accounts, or production settings were changed.

Storage and PostgreSQL cannot share an atomic transaction: successful object
deletions are not undone if a later object deletion or DB commit fails. Rows are
retained on failure so cleanup can be retried; already removed objects are safe
to delete again. Processing workers use the existing missing-row handling and
orphan sweep for output produced after account cleanup.

## Executed locally

- `python -m pytest -q --cov=app`: **633 passed, 76 skipped**,
  **90.81% coverage**, exceeding the required 80%.
- `npm test`: **157 passed, 3 skipped** (live browser tests require a server).
- `python -m ruff check .`: passed.
- Verification-script and load-safety tests: **10 passed**.
- `git diff --cached --check`: passed before each implementation commit.

The existing WSL virtual environment was missing the current main branch's
declared cryptography dependency; `pip install -e .[dev]` repaired the environment.
An existing JWT test emits a warning for its intentionally short test secret.

## Not executed

There is no listening local PostgreSQL, Redis or object store. Docker Desktop
integration is unavailable in this WSL distro. The guarded PostgreSQL and storage
integration tests were collected but skipped, including new quota concurrency
and account rollback/deletion checks. No staging credentials or real large video
were supplied. S3 staging, production, browser playback and a real 1 GB transfer
are **not verified** by the metadata-only and mocked tests.

## Real S3 acceptance

Use the staging API with a disposable account and a real video over 1 GB. Set
`UPLOAD_TEST_EMAIL` and `UPLOAD_TEST_PASSWORD` in the environment, never in git.

```sh
python deploy/verify_direct_upload.py \
  --api-base https://STAGING_HOST/api/ \
  --origin https://STAGING_HOST \
  --video /path/to/real-video.mp4 \
  --evidence /tmp/sprint5-b-staging.json
```

The script checks preflight PUT CORS and explicitly exposed ETag, sends real
parts, drops/reconstructs its manifest after the first part using the resume
API, sends only remaining parts, completes and waits for processing. It never
sends application cookies to storage or records presigned URLs. It keeps the
test video/job for browser playback verification; delete it after checking.
Use `--minimum-bytes 1073741824` if the evidence must prove a full 1 GiB.
This script exercises resume state; also refresh the actual browser during
transfer, reselect the same video and verify only missing parts are sent.

On isolated test services with migrations applied, run:

```sh
RUN_POSTGRES_TESTS=1 python -m pytest \
  tests/integration/test_multipart_postgres.py \
  tests/integration/test_admin_users_postgres.py \
  tests/integration/test_multipart_storage_live.py -q
```

Then have an operator delete the disposable account: verify 204; its cookie
gets 401; its jobs and sessions are absent; S3 source/output/HLS/thumbnail
objects and incomplete uploads are absent; an unrelated account is unchanged.
Repeat the real large-file upload against production only after the cutover
owner confirms the intended target. Keep actual evidence separate from this
local verification record.

## Remote status

Local commits are complete by feature. The first `git push -u origin
feat/sprint5-part-b` was blocked by automatic approval review, which requires
explicit confirmation that this code may be sent to the configured destination,
`https://github.com/Flickpond/Media_player.git`. Push and PR creation remain pending
that confirmation. No push or PR success is claimed.
