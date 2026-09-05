# Scaling Notes — what it takes to serve 50 concurrent users

**Written:** 5 September 2026 · against `main` @ `0e85e5c`
**Status:** analysis and sprint 2 proposal. Nothing here is implemented.

Companion to [`sprint1-report.md`](sprint1-report.md), which records what exists
today. This one records what would have to change, why, and in what order.

---

## 1. "50 concurrent users" is three different questions

The system responds very differently depending on what those 50 people are
doing, and the number is meaningless without saying which.

| Workload | Load it creates | Verdict today |
|---|---|---|
| **50 watching status** | 50 browsers polling every 2 s = **25 req/s** of small indexed reads | Fine as built. No change needed. |
| **50 uploading at once** | Up to **5 GB** of file bytes in flight through one API process | Falls over. Section 3. |
| **50 transcoding at once** (sprint 2) | ~1–2 CPU cores per job = **50–100 cores** | No single host does this. Section 5. |

The third case is the one worth understanding properly, because the answer is
not "buy a bigger box" — it is "the queue absorbs it and people wait", which is
what the architecture was built for.

---

## 2. Where the system stands today

Measured on 5 Sep 2026 from a clean clone, single machine, Docker Desktop:

| Measure | Value |
|---|---|
| Upload response, 3 MB file over loopback | 95 ms |
| Cold start, six services from wiped volumes | ~25 s |
| Two workers, 8 jobs of 4 MB | split 5 / 4, no affinity |
| Job processing (server-side copy, 2 MB) | ~370 ms |

### A caveat on N1

The sprint plan states N1 as *"upload returns <1 s regardless of file size."*
The 95 ms figure is a 3 MB file over loopback. It does **not** demonstrate the
"regardless of file size" half.

`POST /upload` receives the entire file before it starts pushing to MinIO, so
upload latency scales with file size and network speed. The requirement is
satisfied in spirit — the request never waits for *processing* — but the
literal claim only becomes true with presigned uploads (section 4).

---

## 3. Bottlenecks, in the order they bite

### 3.1 One uvicorn process, no workers — `Dockerfile:17`

```
CMD ["sh", "-c", "alembic upgrade head && uvicorn app.main:app --host 0.0.0.0 --port 8000"]
```

A single Python process with one event loop serves all traffic. Ample for
polling; not for 50 simultaneous multipart uploads.

**Note the trap:** adding `--workers 4` also runs `alembic upgrade head` four
times in parallel, racing on the same database. The migration must move to a
one-shot init service *first*. These two changes are a pair, never one alone.

### 3.2 Every upload is written to the API's disk, then read back

Starlette spools `UploadFile` in memory up to **1 MB**
(`MultiPartParser.spool_max_size = 1048576`) and spills to a temporary file
beyond that. `app/api/uploads.py` then seeks the file to measure it and streams
it to MinIO.

So each upload costs a full write plus a full read on the API container's disk,
and the API holds the bytes for the whole transfer. Fifty concurrent 100 MB
uploads is **5 GB of temp files** on a host the plan sizes at 4 GB / 2 vCPU.

This is the wall you hit first, and it is the reason section 4 leads with
presigned uploads.

### 3.3 Database pool caps at 15 — `app/database.py:18`

```python
engine = create_async_engine(settings.postgres_dsn, pool_pre_ping=True)
```

SQLAlchemy defaults apply: `pool_size=5`, `max_overflow=10`, so **15
connections**. Under 50 concurrent requests, work queues on pool checkout
before PostgreSQL itself is the constraint.

The worker side is fine — `app/worker/db.py:21` uses `NullPool` deliberately,
because each RQ task runs its own `asyncio.run` and a pooled connection cannot
cross event loops.

### 3.4 `GET /jobs` has no limit — `app/repositories/jobs.py:41`

```python
result = await session.execute(select(Job).order_by(Job.created_at.desc()))
```

Every row, every call. `docs/contract.md` deferred pagination explicitly, which
was right for sprint 1. At 50 users generating jobs daily it stops being
deferrable. `created_at` is already indexed, so keyset pagination is cheap.

### 3.5 A wasted MinIO round trip per upload — `app/services/storage.py:66`

`upload_stream()` calls `ensure_bucket()` every time, which issues a
`bucket_exists` call before every single upload. Harmless once, pure waste
after that.

### 3.6 No reaper for stranded jobs

Recorded as a known limitation in the sprint 1 report. At 50 users this stops
being theoretical: any worker crash or restart leaves rows stuck at
`processing` with nobody left to write to them, permanently.

### 3.7 Three separate MinIO clients

`app/services/output_urls.py`, `app/services/storage.py` and
`app/worker/storage.py` each construct their own `Minio(...)`. Not a
performance problem, but three places to change endpoint or credentials, and it
makes the sprint 2 worker rewrite messier than it needs to be.

---

## 4. Sprint 2: what is actually easy

Ordered by payoff per hour.

### Free — already built

`docker compose up --scale worker=N`. No code. Stateless workers with a
conditional-claim state machine are what make this real rather than assumed,
and it is already evidenced at a 5 / 4 split across two replicas.

### Half a day, isolated, no contract change

| Change | Location | Size |
|---|---|---|
| Raise pool to `pool_size=20, max_overflow=10` | `app/database.py:18` | 1 line |
| Cache `ensure_bucket()` after first success | `app/services/storage.py:66` | ~4 lines |
| `uvicorn --workers 4` **and** move `alembic upgrade head` to a one-shot service | `Dockerfile:17`, `docker-compose.yml` | 2 small changes, must land together |

### Half a day, small contract change

Paginate `GET /jobs` with `limit` / `offset` or a keyset cursor. ~10 lines
across `app/repositories/jobs.py` and `app/api/jobs.py`. `created_at` is
indexed so it is cheap. Track E consumes this endpoint, so it needs announcing.

### The one worth real effort — presigned PUT uploads

Have the API issue a presigned MinIO URL and create the job row; the **browser
uploads directly to MinIO**. This takes the API out of the byte path entirely:
no temp files, no double I/O, no memory pressure, and N1 becomes literally true
because the API only ever handles a few hundred bytes of JSON.

It forces one design decision: *if the browser uploads directly, who enqueues?*

| Option | Trade-off |
|---|---|
| Client calls a second endpoint after the PUT succeeds | Simple; ~1 extra route. Fails if the client vanishes mid-upload, leaving an orphan row. |
| MinIO bucket notification → webhook → enqueue | Robust, survives client disconnect. More infrastructure than sprint 2 needs. |

**Recommendation:** the client-callback option for sprint 2, with a sweeper for
orphaned rows that never got their callback — which is the same sweeper the
reaper below needs anyway.

This is a `§3.2` contract change, so it costs tracks B and E plus a team
decision, not just code.

### Already scoped for sprint 2 — the reaper

"Job recovery after a worker crash" is already on the sprint 2 list. Add a
`claimed_at` column and sweep `processing` rows older than a lease window back
to `queued`, or to `failed` after N attempts. This is track A's work, and it
pairs naturally with FFmpeg because long transcodes make crashes far likelier
than fast copies do.

---

## 5. What FFmpeg changes — and it cuts the other way

The most important thing on this page.

**Today the worker never touches the bytes.** `CopyProcessor` in
`app/worker/storage.py` performs a *server-side* MinIO copy — the file never
leaves object storage. That is why `--scale worker=2` is nearly free right now.

**FFmpeg cannot do that.** The worker must download the source, transcode, and
upload the output. Three consequences:

1. **Bytes flow through every worker.** Disk and bandwidth per replica, where
   today there is neither.
2. **Workers become CPU-bound**, roughly 1–2 cores per transcode. Two workers
   on a 2 vCPU host will contend for CPU, where today they do not.
3. **Your cheapest scaling lever gets more expensive, not less.** "Scale the
   workers" stops being free and starts requiring cores.

### The good news

Track B already wrote `download_file()` and `upload_file()` in
`app/services/storage.py`, with tests — and nothing in production code uses
them yet. They are exactly the primitives FFmpeg needs.

So the sprint 2 worker change is smaller than it looks: implement
`FfmpegProcessor` behind the existing `ProcessingStep` protocol, using B's
existing methods, and swap it in `get_processing_step()`. The protocol seam was
designed for precisely this. Consolidating the three MinIO clients (3.7) at the
same time makes it cleaner still.

### Sizing with real transcoding

Do **not** size by user count. Size the worker pool by acceptable wait time.

Fifty simultaneous transcodes would need 50–100 cores, which no single droplet
provides. But the system does not fail under that load — it queues, and people
wait. That is the whole point of separating the queue from the request path,
and it is the strongest thing to say about this architecture in the report or
an interview.

The upgrade path is autoscaling workers on queue depth, which is exactly the
Kubernetes HPA story the plan already parks for the December fork.

---

## 6. Beyond sprint 2 — deliberately not now

| Change | Why it waits |
|---|---|
| Object storage → DigitalOcean Spaces or S3 | MinIO on one droplet volume is a single point of data loss. Same S3 API, so it is a config change, but it needs a migration plan. |
| PostgreSQL → managed, with PgBouncer | Connection pooling stops mattering until the API is horizontally scaled. |
| CDN in front of output objects | Playback traffic never touching the application host. Only worth it with real users. |
| Kubernetes with HPA on queue depth | The module explicitly scopes out orchestration. This is the December fork. |

---

## 7. Recommendation for sprint 2

Sprint 2 already carries FFmpeg **and** retries/crash-recovery. That is a full
sprint on its own.

**Do:**

- The four small fixes in section 4 — roughly half a day in total
- The reaper, which is already scoped and is track A's work
- Consolidate the three MinIO clients while doing the FFmpeg swap

**Do only if FFmpeg lands early:**

- Presigned PUT uploads

**Do not:**

- Kubernetes, managed PostgreSQL, or a CDN. The plan is right to park these.

### Sizing

4 GB / 2 vCPU is undersized the moment uploads overlap. With presigned uploads
and storage off-box, 8 GB / 4 vCPU serves 50 concurrent users on copy jobs
comfortably. With FFmpeg, stop sizing the host and start sizing the worker pool
against an acceptable queue wait.

---

## Revision log

| Date | Author | What changed |
|---|---|---|
| 5 Sep 2026 | @1brahim74 | Initial analysis: the three workloads, seven bottlenecks with locations, sprint 2 proposal, FFmpeg impact. |
