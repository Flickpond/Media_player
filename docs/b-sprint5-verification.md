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
- The direct upload API accepts empty or generic MIME types. Actual bytes and
  sizes are checked at completion; invalid content is removed without a job.
  Stored metadata remains octet-stream and attachment.

## Integration handoff and scope

B supplies the operator user list and deletion API. E owns the admin panel
button and browser upload path. This branch leaves `frontend/*`, `load/fixture.py`,
the original `app/api/uploads.py`, existing authentication repository/schema,
and their pre-existing tests exactly as on the base commit. Account repository
and response additions live in new B files, `app/repositories/admin_users.py`
and `app/schemas/admin_user.py`.

The old `POST /upload` remains available. E must switch unknown MIME files
and the frontend fallback to direct upload before B retires that endpoint;
the load fixture must migrate at that coordinated handoff. This part of the
Sprint 5 retirement is pending and is not claimed complete.

Shared production-file changes are limited to:

- `app/main.py`: register the new B account router (two lines).
- `app/api/jobs.py`: extract the existing storage cleanup so B account deletion
  can reuse it. The original best-effort individual deletion behavior remains.
- `app/schemas/upload.py`: allow empty/omitted MIME metadata for B direct uploads.

The old-cookie 401 check is a test of existing auth behavior, not a change
to authentication, token lifetime or role checks. No worker, infrastructure,
live account or production settings were changed.

Storage and PostgreSQL cannot share an atomic transaction: successful object
deletions are not undone if a later object deletion or DB commit fails. Rows are
retained on failure so cleanup can be retried; already removed objects are safe
to delete again. Processing workers use the existing missing-row handling and
orphan sweep for output produced after account cleanup.

## Executed locally

After narrowing the scope:

- `python -m pytest -q --cov=app`: **654 passed, 76 skipped**, **91.02% coverage**.
- `python -m ruff check .`: passed.
- `git diff --exit-code 735da33 -- frontend load/fixture.py app/api/uploads.py
  app/repositories/users.py app/schemas/user.py tests/test_uploads.py
  tests/test_authorization.py README.md`: passed; all these files match the base.
- Frontend tests were not rerun because its source and tests were restored
  exactly to the base commit. Earlier frontend results applied to the previous
  implementation, not this narrowed version.

The existing WSL virtual environment was missing the current main branch's
declared cryptography dependency; `pip install -e .[dev]` repaired the environment.
An existing JWT test emits a warning for its intentionally short test secret.

## Account cleanup follow-up

An individual job delete commits its row before best-effort storage cleanup.
If that storage operation fails, a ready/completed upload session can outlive
the deleted job and still identify its source object. Account deletion now
cleans that known source before deleting any rows. A failed cleanup returns
503 and preserves account/session rows so the operator can retry. Sessions
whose jobs still exist continue through the shared job cleanup path.

Validation: `python -m pytest tests/test_admin_users.py tests/test_jobs_api.py
tests/test_multipart_uploads.py tests/test_upload_limits.py -q`: **79 passed**.
Ruff passed for the changed account API and tests. The four new regression
cases cover both ready/completed sessions, cleanup ordering, safe errors,
database-record preservation and successful retry. This is local mocked
storage evidence, not a claim of live S3 or PostgreSQL execution.

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

For an actual stop between two CLI runs, add `--stop-after-first-part` to the
command above. It writes the session ID to the evidence file before sending
bytes, verifies the first part and exits with status `paused`. Run the same
command again with `--resume`, using the same evidence path. It reads existing
parts from the API and uploads only missing parts; it does not create a second
session. You can also use `--resume` after a network failure or Ctrl+C.

Resume requires the same filename, size, full-file SHA-256, API target, browser
origin and account. Evidence writes use a temporary file and atomic replacement.
A lost completion response resumes `completing`/`ready` with the saved server
manifest; an already completed session returns its existing job ID. Neither
case retransmits video parts.

The script checks preflight PUT CORS and explicitly exposed ETag, sends real
parts, completes and waits for processing. It never sends application cookies
to storage or records presigned URLs. It keeps the test video/job for browser
playback verification; delete it after checking. Failed or deliberately paused
sessions are retained for resume and consume an upload slot until completed,
cancelled through `DELETE /uploads/{id}`, or cleaned after expiration.
Use `--minimum-bytes 1073741824` if the evidence must prove a full 1 GiB.
Also refresh the actual browser during transfer, reselect the same video and
verify only missing parts are sent.

Script follow-up validation: `python -m pytest
tests/test_direct_upload_verification.py -q`: **11 passed**. Tests cover separate
invocations after an intentional pause and a transfer failure, missing-part byte
offsets, CORS and cookie isolation, completion retries, and rejection of changed
bytes or targets. Ruff passed for the script and tests. These mocked checks do
not replace real S3 or browser acceptance.

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

## Git handoff

The user handles all pushes manually. After each new local commit, provide a
copyable `git push origin feat/sprint5-part-b` command; do not run the push.
The branch tracked `origin/feat/sprint5-part-b` at `955baab` when this follow-up
started. Real S3 and production acceptance above are still pending.
