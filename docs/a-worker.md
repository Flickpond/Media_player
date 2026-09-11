# A — Worker + state machine

What track A owes the rest of the team, plus how to run and test the worker.

Written for sprint 1 and kept current: the integration parameters below are
still in force, and sprint 2 changed what "processing" means without changing
any of them. That was the point of the `ProcessingStep` seam.

## Integration parameters (the §4 list)

### Redis queue name

`video_jobs` — the default of `REDIS_QUEUE`, already set in `docker-compose.yml`
for both the `api` and `worker` services.

**B: do not hardcode this.** Call `app.queue.enqueue_job(job_id)` and the queue
name comes from the same settings object the worker reads. That is the whole
point of `app/queue.py` existing — a mismatch between enqueue target and
consume target is silent, the job just sits there.

### Job payload signature

One argument, the job id as a string:

```python
process_job(job_id: str) -> str          # "app.worker.tasks.process_job"
```

The queue entry carries **only the job id**. Everything else the worker needs
(`source_key`, `filename`) it reads from the row B already inserted, so the
queue never holds a second copy of state that can go stale.

The RQ job id is set to the database job id, so a queue entry can be traced
back to its row without a lookup table.

B's call is:

```python
from app.queue import enqueue_job

job = await create_job(session, filename=..., source_key=...)
enqueue_job(job.id)
```

### Write ownership — confirmed

The worker writes `status`, `output_key`, `error`, `updated_at` and nothing
else. Every write goes through `mark_processing` / `mark_done` / `mark_failed`
in C's repository; there is no raw SQL anywhere in `app/worker/`. It never
touches `id`, `filename`, `source_key` or `created_at` (N4).

### Output key convention

```
outputs/{job_id}/{original filename}
```

Derived from the basename of `source_key`, so it follows whatever B chooses for
`uploads/...` without needing a second agreement. E reads `output_url` from the
API, never this key.

## What the worker does

```
queued ──mark_processing──► processing ──FFmpeg transcode──► done
                                 │
                                 └── exception ──► failed (with readable error)
```

"Processing" is an **FFmpeg transcode to 720p H.264 + AAC** (`FfmpegProcessor`),
as of sprint 2. It downloads the source, runs FFmpeg against a temp file, and
uploads the result.

It replaced `CopyProcessor` — a server-side object copy standing in for real
transcoding — and **nothing else changed**: `app/worker/tasks.py` was not
modified, because `ProcessingStep` is the seam and `get_processing_step()` is
the only thing that names an implementation. `CopyProcessor` is still in the
tree and still tested; it is the reference for what a processing step has to
look like.

A failure reaches the user through the job's `error` column, which is **not**
`str(exception)`. See [T-22](known-traps.md#t-22): `ObjectStoreError` carries a
diagnostic for the log and a `user_message` for the screen, and both are
required at every raise site.

### The two design-note cases, already handled

**A job delivered twice.** Claiming is a conditional update: `UPDATE ... WHERE
id = ? AND status = 'queued'`. Exactly one worker wins. The loser gets
`InvalidJobTransitionError`, logs it at info, and returns without touching the
row or re-running the copy. A job that is already `done` is left exactly as it
was — the output key from the first run stands.

**A job that fails.** Any exception from the processing step is converted to a
readable string and written to `error` with status `failed`. It never sits in
`processing` because of an exception this process can see (N3). What is *not*
covered in sprint 1: a worker killed mid-job leaves the row in `processing`
with no one to write to it — there is no reaper and no retry, by design. That
is the `docker kill` demo on Thursday and a sprint 2 item.

### Logging (N9)

Every transition logs at info with the job id:

```
job 6f1c... : queued -> processing (source_key=uploads/6f1c.../demo.mp4)
job 6f1c... : processing -> done (output_key=outputs/6f1c.../demo.mp4)
```

## Running it

```bash
docker compose up -d                    # worker starts with everything else
docker compose up -d --scale worker=2   # N2 evidence: two replicas, one queue
docker compose logs -f worker
```

The replica count lives on the command line, not in `docker-compose.yml`, so a
plain `docker compose up -d` silently scales the workers back to **one**. Pass
`--scale worker=2` on every deploy until that moves into the compose file.

Locally, without containers:

```bash
python -m app.worker            # long-running
python -m app.worker --burst    # drain the queue and exit
```

The worker forks per job on Linux and runs in-process on Windows (no `fork`),
so a teammate on Windows can still run it against the Compose Redis.

## Tests

```bash
pytest tests/test_worker_tasks.py tests/test_worker_storage.py tests/test_queue.py
```

No containers needed — the state machine runs against a fake that imitates the
repository's conditional-update semantics, and `tests/test_queue.py` runs a
real RQ worker against an in-memory Redis.

With the stack up, the same state machine runs against real PostgreSQL:

```bash
RUN_POSTGRES_TESTS=1 pytest tests/integration/test_worker_postgres.py
```

That file is what proves the fake tells the truth. CI runs it on every pull
request, against a real PostgreSQL and a real MinIO.

## The reaper

A worker that is killed mid-job leaves its row in `processing` forever — the
process that owed the transition is gone. `app/worker/reaper.py` runs as its own
container and does two things on a loop:

1. fails rows that have sat in `processing` past their lease, with
   `"worker stopped responding; job was not completed"`
2. deletes objects under `uploads/` that no job row refers to — the orphans
   left when upload is interrupted between writing the object and committing
   the row

Both use a **conditional UPDATE**, so two reapers racing cannot both claim the
same row. `tests/test_reaper.py` covers the orchestration with fakes;
`tests/integration/test_reaper_postgres.py` covers the data paths against real
services, and deliberately never calls `run_once()` — that sweeps the whole
table and bucket, which against a shared stack would reap a teammate's work.

## Still open

- No retries. A failed job stays failed; the user re-uploads.
- The reaper's lease is a fixed timeout, not a heartbeat, so a genuinely slow
  job can be reaped mid-flight. The FFmpeg timeout is set below the lease to
  make that unlikely rather than impossible.
