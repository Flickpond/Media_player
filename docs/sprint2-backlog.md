# Sprint 2 Backlog — what the sprint 1 review left open

**Written:** 7 September 2026 · against `main` @ `40d5e6b` plus the review's fixes
**Status:** P1, P2, P6 and P9 are open. Everything else here is done.

Companion to [`sprint1-report.md`](sprint1-report.md), which records what exists,
and [`scaling-notes.md`](scaling-notes.md), which records what serving 50 users
would take. This one records what the review found and did not fix.

Problems carry stable ids — **P1** to **P9** — so they can be referred to
without quoting section numbers. The ids do not change when this document is
reorganised.

---

## 1. Where this came from

A security review and a general code review of everything merged into `main`
after all five tracks landed — A through E, plus assembly. All branches were
strictly behind `main` at the time, so reviewing `main` covered every track's
work.

---

## 2. Fixed in sprint 1 — do not re-do these

### Security findings

| Finding | Fix | Where |
|---|---|---|
| MinIO published on `0.0.0.0` with default admin credentials | Every published port bound to `127.0.0.1` | `docker-compose.yml` |
| Client-supplied `Content-Type` stored and served back — stored XSS | Allowlist on the declared type, plus content sniffing | `app/api/uploads.py` |
| Uploads accepted with no check on what the file actually is | `sniff_video_type()` reads the container signature; the sniffed type is stored, not the client's claim | `app/services/media_type.py` |
| Unsanitised filename interpolated into the object key | `safe_filename()` — last path segment, charset filter, length cap | `app/api/uploads.py` |
| Redis reachable with no authentication | `requirepass`, threaded through both clients and the healthcheck | `docker-compose.yml` |
| Redis password not URL-encoded in the connection string | `quote(..., safe="")` | `app/config.py` |
| Size limit enforced only after the whole body was spooled to disk | Content-Length guard in middleware, before the body is parsed | `app/main.py` |
| Zero-byte uploads accepted and processed to `done` | Closed incidentally — too short to sniff | `app/services/media_type.py` |

### Code review findings

| Id | Problem | Fix |
|---|---|---|
| **P3** | `--burst` silently used the non-forking worker; the comment only explained the Windows half | Comment now explains both conditions. Behaviour unchanged — it was deliberate — and pinned by its own test rather than riding on the Windows one. |
| **P4** | The frontend polled forever on a stuck job, and left the upload button disabled with it | After 30 polls the page says it is taking longer than expected and hands the form back. It keeps polling and never claims failure, because it cannot tell "stuck" from "slow". |
| **P5** | `GET /jobs` had no `LIMIT` and signed URLs one row at a time | `limit` (default 50, max 200) and `offset`, with `id` breaking ties on `created_at` so page boundaries are stable. Signing is now batched with `asyncio.gather`. [`contract.md`](contract.md) updated. |
| **P7** | Containers ran as root | Non-root `flickpond` user (uid 10001) after the install step. `/app` stays root-owned and read-only to it. |
| **P8** | The engine was built at import time, so nothing under `app/` could be imported without a resolvable DSN | `get_engine()` / `get_session_factory()`, cached and built on first use — the shape `app/worker/db.py` already had. |

P5 and P8 were pulled into sprint 1 specifically because deferring them costs
more than doing them: P5 is a public API contract that gets harder to change as
consumers appear, and P8 is an import-time coupling that widens with every new
importer. P3 and P7 were near-free. P4 was timing — see the note under P2 about
the `docker kill` demo.

---

## 3. P1 — No authorization

**Severity: high. Blocks any deployment that is not loopback-only.**

`GET /jobs` returns rows from the whole table, each with a live signed download
URL. `GET /jobs/{id}` is equally open. There is no authentication anywhere in
the API and no owner column on `jobs`, so there is nothing to scope a query to
even if there were a caller identity.

This is a design gap, not a coding slip — sprint 1 scoped auth out
deliberately. It only becomes exploitable the moment the API is reachable by
someone who should not see everything.

**Mitigated two ways, neither of them a fix.**

1. Every service except nginx is pinned to `127.0.0.1`, and that is no longer
   configurable — see the port policy in the README. nginx proxies `/api/` and
   `/videos/` over the compose network, so nothing else needs a host port.
2. A published deployment puts HTTP basic auth in front of the whole server via
   `deploy/auth` — the page, `/api/` and `/videos/` alike. See
   [`../deploy/auth/README.md`](../deploy/auth/README.md).

Both are deployment controls. Basic auth is a *shared* password: it tells you
nobody uninvited got in, not who did what, and it cannot express "this user may
see their own jobs". Pagination (P5) caps how much leaks per request; it does
not stop the leak. Delete the gate when this lands.

This was not hypothetical. The Alibaba deployment ran for a time with
PostgreSQL, Redis and the MinIO console on `0.0.0.0` and the API reachable
through nginx, and `GET /api/jobs` served every job with a working signed
download URL to anyone who found the address.

**What it takes:**

1. An `owner_id` column on `jobs`, plus a migration.
2. A caller identity — session or token — and a dependency that resolves it.
3. `list_jobs` scoped to that owner; `get_job` returning 404 (not 403) across
   owners, so job ids cannot be probed.
4. A decision on the operator view. The sprint 1 plan has an operator story
   ("see all jobs and their status") which is exactly the endpoint that has to
   stop being public — it needs a role, not deletion.

Item 4 is the one worth deciding early, because it changes the shape of the
endpoint rather than just adding a filter.

---

## 4. P2 — The upload is not atomic

**The highest-value correctness item left.**

`POST /upload` writes to three systems in sequence, with no transaction
spanning them:

```
1. storage.upload_stream(...)   → object lands in MinIO
2. create_job(...)              → row lands in PostgreSQL
3. enqueue_job(job_id)          → id lands in Redis
```

Atomic would mean all three happen or none do. They are three independent
writes to three independent systems, so any of them can fail — or the API
process can die — after the earlier ones have already committed. Nothing
unwinds what already succeeded.

Two failure modes, both silent:

**Step 2 fails after step 1.** The object is in MinIO with no row pointing at
it. Nothing references it, nothing will ever look for it, and no cleanup exists
— it occupies storage permanently. The user sees a 500 and retries, creating a
second object. Storage grows with garbage nobody can attribute to anything.

**Step 3 fails after step 2.** The row exists at `queued`, but nothing is on
the queue. No worker will ever pick it up, because a worker only learns about
jobs from Redis. The row sits at `queued` forever. To the user and to the
operator dashboard this is indistinguishable from "the queue is busy" — the
status is a legitimate state, just one it will never leave.

The ordering comment in `app/api/uploads.py` explains why the enqueue goes
*last* — so a fast worker cannot read a row that does not exist yet. That
reasoning is correct and should stay. What is missing is any handling of the
case where a step fails.

### Three ways to fix it

- **Compensating actions.** Wrap each step: if the insert fails, delete the
  object; if the enqueue fails, mark the row `failed` so the user is told
  rather than left polling. Cheap, closes the visible symptom, does not survive
  the process being killed between two steps.
- **A reconciliation sweeper.** A periodic pass that re-enqueues `queued` rows
  older than *N* minutes with no queue entry, and deletes objects with no
  matching row. Survives crashes, because it does not depend on the request
  surviving.
- **The outbox pattern.** Write the job row and an outbox entry in one database
  transaction; a separate dispatcher reads the outbox and publishes to Redis.
  The enqueue then cannot be lost. Correct, and the most work.

### What each costs

Three facts in the current code drive these numbers:

- There is no `queued -> failed` transition — `mark_failed` requires
  `expected_status=PROCESSING`. Worse, [`contract.md`](contract.md) states that
  only the worker may change `status` after the API's insert (N4). Having the
  API fail its own upload therefore needs that shared rule amended, not just a
  new repository function.
- `StorageService` has no delete method; cleaning up an orphan means adding one.
- **The RQ job id is already the database job id** (`app/queue.py`), so a
  sweeper can ask `Job.exists(str(job_id))` directly — no lookup table, no
  scan, no correlation logic. This is what makes the sweeper cheap, and it is a
  free consequence of a decision already made in sprint 1.

| Approach | Code | Coordination | Covers | Total |
|---|---|--:|---|---|
| Compensating actions | ~0.5 day | **High** — amends the write-ownership rule, needs A and C to agree | Exceptions only; a SIGKILL between steps still strands things | 0.5 day + a team decision |
| **Reconciliation sweeper** | ~1.5–2 days standalone | None — worker-side, contract untouched | Both failure modes, survives crashes | **~0.5 day marginal** |
| Outbox pattern | ~2–3 days | Low | **Only the enqueue gap** — the orphaned object is untouched | 2–3 days, still incomplete |

**The sweeper's real cost is the marginal one.** Sprint 1 already has a planned
reaper for rows stranded in `processing` by a killed worker. A sweeper that
re-enqueues stranded `queued` rows is the same component with one more query —
the periodic runner, the compose service, the query scaffolding and the test
harness are all shared. That is the reason to prefer it over compensating
actions, and the reason deferring P2 is *cheaper* than doing it now.

### Why "atomic" is the wrong target

True atomicity across MinIO, PostgreSQL and Redis is not achievable at sane
cost — it needs distributed transactions, which is not a reasonable thing to
introduce here. Note that the outbox pattern, the textbook answer, still only
fixes one of the two failure modes.

The achievable goal is **no silent failure states**: every partial failure
discoverable and recoverable, even though partial failures will still happen. A
`queued` row a sweeper re-enqueues is fine. An orphaned object a sweeper deletes
is fine. What is not fine is the current situation, where both are invisible
permanently. That is why the sweeper is the correct design rather than a cheaper
approximation of atomicity.

One option to avoid: writing the row first in a `pending` state. It sounds
cheap, but it adds a fifth job state — the enum, the CHECK constraint, a
migration, the contract, and every consumer that switches on status. More work
than the sweeper for a worse outcome.

**Demo note:** P4 now surfaces a stuck job in the UI after a minute, so the
`docker kill` demonstration shows the page noticing rather than spinning
silently. The row is still stranded — that is P2, and it is still the finding.

---

## 5. Smaller open items

| Id | Problem | Where | Note |
|---|---|---|---|
| **P6** | `ensure_bucket()` runs on every upload | `app/services/storage.py` | An extra round-trip on a path budgeted under 1 s (N1). Belongs in a startup hook. Flat cost — no urgency. |
| **P9** | Exception class names and object keys reach the user-facing `error` column | `app/worker/tasks.py`, `app/worker/storage.py` | Fine for a demo. Split the user-facing message from the logged diagnostic before real users see it. |

---

## 6. Accepted, with reasons

**Dev credentials for MinIO and PostgreSQL remain in `docker-compose.yml`.**
Both stores are loopback-bound, which is what makes this tolerable, and it keeps
`docker compose up` working on a fresh clone with no `.env`. `.env.example`
says to change them before publishing the ports. If the team would rather fail
loudly on an unset credential, make the settings required fields — a small
change, but it breaks the zero-config clone.

**Content sniffing proves the container, not the stream.** A file carrying a
valid `ftyp` header followed by garbage is accepted. Detecting that needs a real
demux, which arrives for free when FFmpeg replaces the copy step — a file that
cannot be decoded will fail the job with a readable error, which is the correct
outcome anyway. Worth stating plainly if anyone asks whether uploads are
"validated": the container is checked, the content is not.

**A declared type that disagrees with the sniffed type is not treated as an
attack.** Browsers routinely mislabel containers by extension, so the bytes
simply win and the file is stored as what it is. Rejecting on mismatch would
fail real users for no security gain.

**`ruff format` is not clean on two files** (`app/services/storage.py`,
`tests/integration/test_storage.py`). Both predate this review, and the project
gates on `ruff check`, not `ruff format`. Left alone rather than reformatting
other people's files for no functional gain.

---

## 7. Suggested order

1. **P1, authorization** — the only item that blocks deployment.
2. **P2 via the sweeper**, built together with the already-planned `processing`
   reaper. Two limitations, one component.
3. **P6 and P9** as hygiene, whenever those files are open anyway.
