# Sprint 1 Integration Contract

This document is the shared boundary for the API, worker, database, storage, and frontend during Sprint 1.

## Job states

The only valid states are `queued`, `processing`, `done`, and `failed`.

The API creates a job in `queued`. After creation, only the worker may change `status`, `output_key`, `error`, or `updated_at`.

Valid one-way transitions are:

```text
queued -> processing -> done
                     -> failed
```

Sprint 1 has no retries. A failed job must contain a readable error.

## Jobs table

| Column | Type | Rule |
| --- | --- | --- |
| `id` | UUID | Primary key |
| `filename` | Text | Original filename |
| `status` | Text | One of the four states above |
| `source_key` | Text | MinIO input object key |
| `output_key` | Text, nullable | MinIO output object key; null until `done` |
| `error` | Text, nullable | Present only for `failed` jobs |
| `created_at` | Timestamp with time zone | Set when the API creates the job |
| `updated_at` | Timestamp with time zone | Updated on every worker transition |

The table has indexes on `status` and `created_at`.

## HTTP API

```text
POST /upload
  202 { "job_id": "<uuid>" }
  400 { "error": "file too large" }

GET /jobs/{id}
  200 { "id", "filename", "status", "output_url"?, "error"? }
  404 { "error": "not found" }

GET /jobs
  200 [ ...same job shape... ]
```

The API never returns `source_key` or `output_key`. For a completed job, it converts `output_key` to a time-limited MinIO `output_url`. Null optional fields are omitted from JSON.

`GET /jobs` returns newest jobs first. Pagination is outside Sprint 1 scope.

## Repository interface

The shared asynchronous repository functions are:

```python
create_job(session, *, filename, source_key, job_id=None)
get_job(session, job_id)
list_jobs(session)
mark_processing(session, job_id)
mark_done(session, job_id, *, output_key)
mark_failed(session, job_id, *, error)
```

The worker is the sole caller of the three `mark_*` functions. Each transition is an atomic conditional update, so an invalid or repeated transition fails rather than silently overwriting state.

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
