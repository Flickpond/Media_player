# A — Worker + state machine

Everything in §4 of the sprint plan that A owes the rest of the team, plus how
to run and test the worker.

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
queued ──mark_processing──► processing ──copy object──► done
                                 │
                                 └── exception ──► failed (with readable error)
```

"Processing" is a **server-side object copy** in MinIO, standing in for FFmpeg.
Sprint 2 replaces `CopyProcessor.run()` and nothing else — the `ProcessingStep`
protocol is the seam.

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
docker compose up -d                 # worker starts with everything else
docker compose up -d --scale worker=2   # Thursday's N2 evidence
docker compose logs -f worker
```

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

That file is what proves the fake tells the truth. **Run it once C's migration
is live on D's Postgres (Mon 7 Sep)** — until then the worker's DB path is
unverified against anything but a fake.

## Still open

- Not yet run against a real MinIO or a real enqueue from B's endpoint. The
  seam is covered by tests on both sides, but Tuesday 8 Sep is when this gets
  proven for real.
- No retry or crash recovery — deliberately out of scope (sprint 2).
