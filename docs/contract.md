# Sprint 1 Integration Contract

This document is the shared boundary for the API, worker, database, storage, and frontend during Sprint 1.

## Job states

The only valid states are `queued`, `processing`, `done`, and `failed`.

The API creates a job in `queued`. The worker owns processing transitions;
Sprint 3 adds one explicit API exception: an authenticated owner can retry
their failed job through `prepare_retry`, changing `failed -> queued`.
All of these writes go through `app/repositories/jobs.py`.

Valid one-way transitions are:

```text
queued -> processing -> done
                     -> failed
```

Sprint 1 had no retries. Sprint 3 adds `failed -> queued` through
`POST /jobs/{id}/retry`; a failed job must still contain a readable error.
Retry preserves the job ID, owner, source and operations, clears `error`,
`output_key` and `hls_key`, and advances `updated_at`.

**`error` is read by the person who uploaded the file**, because
`GET /jobs/{id}` returns it verbatim. It must name a cause they can act on and
must not carry an object key, a temp path, an S3 code or an exception class
name. The diagnostic version of the same failure goes to the worker log. In
code that split is `ObjectStoreError`: `str(exc)` is the operator's half,
`user_message` the uploader's, and both are required.

## Jobs table

| Column | Type | Rule |
| --- | --- | --- |
| `id` | UUID | Primary key |
| `owner_id` | UUID | Who uploaded it. FK to `users.id`, indexed, NOT NULL |
| `filename` | Text | Original filename |
| `status` | Text | One of the four states above |
| `source_key` | Text | MinIO input object key |
| `output_key` | Text, nullable | MinIO output object key; null until `done` |
| `hls_key` | Text, nullable | MinIO key of the HLS master playlist. Null when no ladder was built — including on a `done` job, since the ladder is best-effort and `output_key` is the fallback |
| `operations` | JSONB, nullable | Null on an upload. On an edit job, the operations requested. **The array's order is not execution order** — the worker runs clip → crop → scale → convert regardless |
| `error` | Text, nullable | Present only for `failed` jobs. **User-facing** — see the rule above |
| `created_at` | Timestamp with time zone | Set when the API creates the job |
| `updated_at` | Timestamp with time zone | Updated on every worker transition |

The table has indexes on `status` and `created_at`.

## Authentication

Every job endpoint requires a caller. Authentication uses signed JWTs per
proposal §5.4, delivered in an **HttpOnly cookie** rather than an
`Authorization` header, so the token is not reachable by script.

```text
POST /auth/register   201 { id, email, role }   409 if the email is taken
POST /auth/login      200 { id, email, role }   401, identical for a wrong
                                                password and an unknown email
POST /auth/logout     204, clears the cookie
GET  /auth/me         200 { id, email, role }   401 if not signed in
```

`/auth/me` exists because the page cannot read an HttpOnly cookie, and logout
is a server endpoint for the same reason -- a script cannot delete one.

Roles are `user` and `operator`, constrained in the database. A job belonging to
another user is **404, not 403**: a 403 confirms the id exists and turns the
endpoint into a way to discover valid job ids. 403 is only for a role check.

`JWT_SECRET` has no default and must be at least 32 bytes; the API refuses to
issue or accept a token without one.

## HTTP API

```text
POST /upload
  202 { "job_id": "<uuid>" }
  413 { "error": "file too large" }
  415 { "error": "unsupported media type" }                          // declared Content-Type not allowed
  415 { "error": "file content is not a recognized video format" }   // sniffed bytes disagree

GET /jobs/{id}
  200 { "id", "filename", "status", "output_url"?, "error"? }
  404 { "error": "not found" }

POST /jobs/{id}/retry
  202 { "job_id": "<same uuid>" } // no request body; only the owner's failed job
  404 { "error": "not found" }    // missing or another owner's job
  409 { "error": "only failed jobs can be retried" }
  503 { "error": "retry could not be confirmed; refresh the job and try again" }

DELETE /jobs/{id}
  204                          // no body. Deletes regardless of status.
  404 { "error": "not found" } // unknown id, or not this caller's job -- same
                                // as GET, never 403: a 403 confirms the id
                                // exists and lets someone probe for valid ids.

GET /jobs?limit=<1..200>&offset=<n>
  200 [ ...same job shape... ]   // the caller's own jobs only
  401 { "error": "not authenticated" }
  422 { "detail": [...] }   // limit or offset out of range

GET /admin/jobs?limit=<1..200>&offset=<n>
  200 [ ...same job shape... ]   // every owner's jobs
  403 { "error": "operator role required" }

DELETE /admin/jobs/{id}
  204                          // unscoped -- deletes any user's job
  403 { "error": "operator role required" }
  404 { "error": "not found" } // unknown id only; ownership is never checked
```

`GET /admin/jobs` is sprint 1's operator story ("see all jobs, so I can spot
stuck jobs"), which used to be what `GET /jobs` did for everybody. It is a
separate route rather than a role branch inside `GET /jobs`: one URL that means
different things depending on who asks is easy to get wrong and easy to
mis-test.

The API never returns `source_key` or `output_key`. For a completed job, it converts `output_key` to a time-limited MinIO `output_url`. Null optional fields are omitted from JSON.

`GET /jobs` returns newest jobs first, ordered by `created_at` descending with `id` breaking ties so page boundaries are stable.

Both query parameters are optional: `limit` defaults to 50 and is capped at 200, `offset` defaults to 0. A caller that passes neither gets the first 50 rows — this is the one behaviour that changed after Sprint 1's review, and it is deliberate: the endpoint mints a signed URL per row returned, so an uncapped list makes its cost grow with the table.

## Repository interface

The shared asynchronous repository functions are:

```python
create_job(session, *, owner_id, filename, source_key, job_id=None)
get_job(session, job_id, *, owner_id=None)  # None = any owner (worker, reaper)
list_jobs(session, *, owner_id=None, limit=50, offset=0)  # None = every owner
mark_processing(session, job_id)
mark_done(session, job_id, *, output_key)
mark_failed(session, job_id, *, error)
prepare_retry(session, job_id, *, owner_id)  # API: enqueue, then commit; rollback on failure
```

The worker is the sole caller of the three processing `mark_*` functions.
Each transition is a conditional update. `mark_processing` first locks the
row by ID so it waits for a pending retry transaction, even while the
committed snapshot still says `failed`.

`prepare_retry` leaves a transaction open deliberately: the caller enqueues
before committing. An enqueue failure rolls back the entire change, retaining
the original failed row. This is not a distributed transaction with Redis;
after a 503, refresh the job before retrying. Never commit this preparation
without attempting queue delivery.

## Sprint 3 crop/clip validation seam

Track C supplies `app/worker/validation.py`; B's edit processor calls:

```python
validate_crop(params: dict, probe: SourceProbe) -> None
validate_clip(params: dict, probe: SourceProbe) -> None
```

Both raise `ValueError` with a curated, user-readable message on failure.
The probe describes the real input, after download and before constructing
FFmpeg arguments. A rectangle outside the frame or an end time beyond the
source duration fails the worker job; it is **not an HTTP 422**. API request
shape, supported operation names, and downscale/upscale conflicts remain B's
responsibility. See [C's handoff](c-recovery-input-safety.md) for the required
`ObjectStoreError` adapter that preserves these messages in `GET /jobs/{id}`.

## Shared configuration

| Variable | Purpose |
| --- | --- |
| `POSTGRES_DSN` | PostgreSQL connection string |
| `MINIO_ENDPOINT` | MinIO host and port |
| `MINIO_PUBLIC_ENDPOINT` | Browser-accessible MinIO host and port used in signed URLs |
| `MINIO_ACCESS_KEY` | MinIO access key |
| `MINIO_SECRET_KEY` | MinIO secret key |
| `MINIO_BUCKET` | Bucket containing video objects |
| `MINIO_REGION` | Object-storage region used when signing URLs |
| `MINIO_USE_SSL` | Whether the MinIO connection uses TLS |
