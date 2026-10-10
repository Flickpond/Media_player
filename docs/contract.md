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
| `hls_status` | Text, NOT NULL, default `pending` | `pending`, `ready` or `unavailable`. **`ready` exactly when `hls_key` exists** — enforced by `ck_jobs_hls_ready_has_key`, so the two are always written together. Always returned by `GET /jobs/{id}` |
| `width`, `height` | Integer, nullable | The real dimensions of what this job plays: the upload for an upload, the edited output for an edit (a crop or scale changes them). Set once the worker has probed it. Positive when set. **As displayed**: a phone video recorded upright is stored as landscape frames plus a rotation flag, and the probe reports it portrait, the way FFmpeg encodes it |
| `duration_seconds` | Float, nullable | The real duration, on the same rule as `width` and `height`. Positive when set |
| `thumbnail_key` | Text, nullable | Object key of the poster frame (`outputs/<id>/thumbnail.jpg`), once extracted. Set by `mark_done` with the MP4 and cleared by every other transition. Best-effort: a done job may have none. The API returns it as a signed `thumbnail_url` |
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
  200 { "id", "filename", "status", "hls_status",
        "output_url"?, "hls_url"?, "width"?, "height"?, "duration_seconds"?,
        "thumbnail_url"?, "edit_options"?, "error"? }
  404 { "error": "not found" }

  edit_options: { "downscale": [480, 360, 240], "upscale": [1080, 1440, 2160] }
                // only on a done job with known dimensions; see "Media rules"

POST /jobs/{id}/retry
  202 { "job_id": "<same uuid>" } // no request body; only the owner's failed job
  404 { "error": "not found" }    // missing or another owner's job
  409 { "error": "only failed jobs can be retried" }
  503 { "error": "retry could not be confirmed; refresh the job and try again" }

POST /jobs/{id}/edit
  { "operations": [ { "operation": "...", "params": { ... } } ] }
  202 { "job_id": "<new uuid>" }
  404 { "error": "not found" } // missing or another owner's source job
  409 { "error": "only completed jobs can be edited" }
  422 { "detail": [...] }       // empty, unknown, malformed, duplicate, or
                                  // downscale and upscale together
  422 { "error": "<readable>" } // breaks a media rule: a scale height that is
                                  // not a rung, not below/above the video, or
                                  // crop/scale with MP3 -- see "Media rules"
  503 { "error": "edit could not be queued; please try again" }

GET /limits                     // public, no sign-in
  200 { "max_duration_seconds": 300, "max_edit_height": 2160 }
                                  // 0 = no duration limit configured

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

GET /jobs/{id}/hls/{path}
  307 -> a freshly presigned, time-limited object URL (Location header)
  404 { "error": "not found" } // unknown id, not this caller's job, no ladder
                                // built, or a `path` outside a ladder's shape
```

**The HLS route.** A ladder is hundreds of objects, so they cannot all be
signed in advance the way `output_url` is. Each part is signed on demand
behind the same ownership check as every other job route, and returned as a
redirect -- segment traffic goes straight from the browser to object storage
and never through the API.

`path` is an allowlist of the shapes FFmpeg writes (`master.m3u8`,
`v0/index.m3u8`, `v0/seg00001.ts`), not a blocklist of traversal tricks. It
is interpolated into an object key, which is where sprint 1's review found
the unsanitised upload filename; anything else is 404, never 400, since a 400
would confirm the job exists and only the path was wrong.

Playlists are **not** rewritten. FFmpeg writes relative segment names, so a
player resolving one against `/jobs/{id}/hls/v0/index.m3u8` asks this same
route for `/jobs/{id}/hls/v0/seg00001.ts`. Serving the ladder under a path
that mirrors its storage layout is what removes the need for any manifest
post-processing.

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
create_edit_job(session, *, owner_id, filename, source_key, operations, job_id=None)
get_job(session, job_id, *, owner_id=None)  # None = any owner (worker, reaper)
list_jobs(session, *, owner_id=None, limit=50, offset=0)  # None = every owner
mark_processing(session, job_id)
mark_done(session, job_id, *, output_key, hls_key=None, hls_status=None,
          width=None, height=None, duration_seconds=None, thumbnail_key=None)
mark_failed(session, job_id, *, error)
mark_ladder_ready(session, job_id, *, hls_key)   # -> Job, or None if not waiting
mark_ladder_unavailable(session, job_id)         # -> Job, or None if not waiting
list_stale_ladders(session, *, before)           # reaper
prepare_retry(session, job_id, *, owner_id)  # API: enqueue, then commit; rollback on failure
```

`hls_key` is optional because the adaptive ladder is best-effort: a `done`
job with no ladder is a legitimate state, and the database carries no
constraint tying the two together. It is written on every transition, so a
retry that produces no ladder clears a stale key rather than leaving it
pointing at segments the new run has overwritten.

**The ladder is its own job** (sprint 4). The MP4 job finishes with
`mark_done(..., hls_status=PENDING)`, commits, and only then queues
`app.worker.tasks.build_ladder` on the `<REDIS_QUEUE>-ladder` queue, under
the RQ id `<job id>-ladder`. Workers listen to the main queue first, so a
ladder never delays somebody else's MP4. The ladder job settles the row with
`mark_ladder_ready` or `mark_ladder_unavailable`; both only match a `done`
job whose ladder is still `pending`, and neither touches `status`. If the
ladder cannot be queued it is settled `unavailable` at once. The reaper
re-queues a pending ladder that Redis has lost and gives up on one whose
queue entry failed, once it has been pending longer than the lease.

The worker is the sole caller of the processing `mark_*` functions.
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

## Sprint 4 media rules (Track C)

The rules live in `app/services/media_rules.py`, which both the API and the
worker read, so the page, the API and the worker can never disagree.
Details and the reasoning: [C's sprint 4 handoff](c-media-rules-thumbnails.md).

**Probe-first rejection.** The MP4 processor probes every upload right after
download and before anything is encoded. A file ffprobe cannot read, one with
no video stream, or one longer than `MEDIA_MAX_DURATION_SECONDS` fails the job
in seconds with a readable `error`:

| Case | `error` |
| --- | --- |
| Corrupt or unreadable | `the video could not be processed; it may be corrupt or in a format we cannot read` |
| No video stream | `this file has no video track; please upload a video file` |
| Too long | `this video is 12:35 long; the limit is 5 minutes. Trim it and upload it again` |

**Thumbnails.** One JPEG per upload, taken one second in (the middle of a
shorter clip), within a 640x360 box, never enlarged. Taken right after the
probe, stored after the MP4 so a failed encode leaves nothing behind, and
recorded by `mark_done`. Best-effort: no thumbnail never fails a job. Edit
jobs have none yet.

**Scale rules.** An edit may scale only to a rung: 240, 360, 480, 720, 1080,
1440 or 2160. Downscale must be below, and upscale above, the height of the
frame it is applied to: the crop's height when there is a crop, otherwise
the height of the copy the edit starts from. Today that copy is the MP4, so
for an upload it is `min(height, WORKER_FFMPEG_MAX_HEIGHT)`; for an edit job
it is the edit's own stored `height`. Crop or scale combined with MP3 is
refused. `edit_options` on the job lists exactly the heights these rules
accept, and the API answers anything else with a 422 before creating a job.
With no stored dimensions the API checks only the rung, and the worker
applies the full rules against the downloaded file (`validate_edit_rules`).
Crop and clip ranges are still checked by the worker only.

```python
validate_source(probe, *, max_duration_seconds) -> None          # A's MP4 processor
validate_edit_rules(operations: list[tuple[str, dict]], probe) -> None   # B's edit processor
```

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
| `STORAGE_USE_INSTANCE_ROLE` | `true` on AWS: S3 credentials come from the instance role, and the two keys above are ignored |
| `WORKER_FFMPEG_MAX_HEIGHT` | Tallest MP4 fallback, default `1080` |
| `WORKER_HLS_MAX_HEIGHT` | Tallest ladder rung, default `1080`; `2160` on a host that can encode 4K inside the job timeout. Never above the source |
| `MEDIA_MAX_DURATION_SECONDS` | Longest source the worker accepts, checked after the probe (default 300; 0 = off) |
| `THUMBNAIL_MAX_WIDTH`, `THUMBNAIL_MAX_HEIGHT` | The poster frame's bounding box (default 640x360) |

## Sprint 4: direct uploads (B/E)

The legacy `POST /upload` contract stays unchanged. New owner-scoped endpoints:

| Method | Path | Request | Success |
| --- | --- | --- | --- |
| POST | `/uploads` | `{filename, size, content_type}` | 201 `{upload_id, part_size, part_count}` |
| POST | `/uploads/{id}/parts` | `{part_numbers: [1, 2]}` | 200 `{urls: {"1": "...", "2": "..."}}` |
| GET | `/uploads/{id}` | none | 200 `{parts_done: [{n, etag}], state, part_size, part_count, job_id}` |
| POST | `/uploads/{id}/complete` | `{parts: [{n, etag}]}` | 202 `{job_id}` |
| DELETE | `/uploads/{id}` | none | 204 |

Parts are 16 MiB except the final part. Upload sessions live for 24 hours;
signed PUT URLs live for at most 15 minutes. Job creation happens only after
completion and content validation; repeating completion returns the same job ID.
The server reserves that ID early without creating a job row. Jobs' existing
state transitions and worker ownership are unchanged.

See [B's handoff](b-direct-uploads.md) for error statuses, frozen completion
manifests, queue recovery, CORS, cancellation and frontend resume behavior.
Migration `20261008_05` creates a separate `upload_sessions` table; deploy it
before the new API/reaper. Existing job metadata is unchanged by this migration.
