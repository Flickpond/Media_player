# Sprint 2 — Plan

**Written:** 10 September 2026 · against `main` @ `559bd3e`
**Last updated:** 10 September 2026 — S2-01, S2-02, S2-04 and S2-05 are **done
and deployed**. Only S2-03 remains a Must. See §4.
**Sprint 1 ended:** 10 September 2026

This document is written to be executed by someone — or something — with no
prior context on this project. It states what exists, what to build, in what
order, and what will bite you.

**Read these three files before writing any code:**

1. This document, at least §1–§3.
2. [`known-traps.md`](known-traps.md) — 21 traps already hit on this project.
   Most of them fail *silently*.
3. [`contract.md`](contract.md) — the shared API and schema boundary. Changing
   it requires telling the team.

---

## 1. What this project is

Flickpond is an asynchronous video upload and processing platform. The request
path is deliberately decoupled from the slow work:

```
browser -> nginx -> FastAPI -> MinIO (store bytes)
                            -> PostgreSQL (create job row, status=queued)
                            -> Redis/RQ (enqueue job id)
                                   |
                                   v
                            worker (1..N replicas)
                                   |
                     queued -> processing -> done | failed
                                   |
                            browser polls GET /api/jobs/{id}
```

**Sprint 1 shipped all of that except real transcoding, and sprint 2 added it.**
The worker now runs FFmpeg (S2-01), a reaper recovers jobs left behind by a
crashed worker (S2-02), and CI gates every PR (S2-04).

### What is already true (do not rebuild these)

| Area | State |
|---|---|
| Upload endpoint | `POST /upload`, 100 MB cap, content sniffing, filename sanitisation |
| Job status API | `GET /jobs` (paginated), `GET /jobs/{id}` |
| Job state machine | `queued -> processing -> done \| failed`, one-way, enforced by DB CHECK constraints |
| Worker | RQ, stateless, scales with `--scale worker=N` |
| Frontend | Upload, 2 s polling, playback |
| Transcoding | FFmpeg → H.264/AAC MP4, capped at 720p, `+faststart` |
| Crash recovery | Reaper service: stale `processing` → `failed`, stale `queued` re-enqueued, orphan objects deleted |
| CI | 5 GitHub Actions jobs, **required** status checks on `main` |
| Deployment | <https://flickpond.com> (Alibaba ECS, cn-hongkong), Let's Encrypt TLS, basic auth gate |
| Tests | 133 unit + 16 integration (Python), 20 unit + 3 live (frontend), 87% coverage |

### What is deliberately not built

No authentication (S2-03) — the biggest remaining gap, and the reason the
deployment sits behind a shared-password gate. No retries: a failed job is
terminal, and the reaper marks a stranded one failed rather than retrying it.
TLS is now in place (S2-05) — the site is at <https://flickpond.com>.

---

## 2. Getting the project running

### Local

```bash
cp .env.example .env
docker compose up --build -d
docker compose ps                 # wait for all healthy
```

Open <http://localhost:3000>. Use `localhost:3000` or `127.0.0.1:3000` exactly —
the CORS allowlist has only those two origins, and opening `index.html` from
disk sends a `null` origin that is always blocked.

### Tests

```bash
# Python unit — no services needed, this is what CI will run
python -m pytest -q

# Python full — needs the stack up
RUN_POSTGRES_TESTS=1 python -m pytest tests/ -q

# Lint (the gate the project actually uses; `ruff format` is NOT enforced)
python -m ruff check .

# Frontend
cd frontend && npm ci && npm test
RUN_LIVE_TESTS=1 npx vitest run     # needs the stack up
```

Coverage gate is `fail_under = 80` in `pyproject.toml`. Actual is 87% on the
unit path. **Do not let it drop below the gate**, and prefer not to let it
drop at all — it was 92% before sprint 2 added code that only integration
tests reach.

### The deployed host

```bash
ssh -i <key>.pem root@47.238.64.156
cd /root/Media_player               # tracks origin/main
```

Datastores are loopback-only by design (T-05). To reach one:

```bash
ssh -L 5432:127.0.0.1:5432 root@47.238.64.156   # then psql localhost:5432
ssh -L 9001:127.0.0.1:9001 root@47.238.64.156   # then MinIO console :9001
```

The site is behind HTTP basic auth. Credentials live only on the host, in
`deploy/auth/htpasswd` as a hash. To rotate:

```bash
./deploy/auth/set-password.sh                    # random password
./deploy/auth/verify.sh http://127.0.0.1 flickpond '<password>'
```

---

## 3. Rules that are not negotiable

1. **Never commit a credential or key.** `*.pem`, `*.key`, `deploy/auth/htpasswd`
   are gitignored. Verify with `git diff --cached --name-only` before every
   commit. See T-16.
2. **Never publish a datastore port.** Only nginx is public. See T-05.
3. **Every file under `tests/integration/` carries the `RUN_POSTGRES_TESTS`
   guard.** See T-11.
4. **The worker is the sole writer** to `status`, `output_key`, `error`,
   `updated_at` after the API's insert. This is contract §N4. The reaper
   (S2-02) is worker-side, so it does not break this.
5. **Transitions are one-way** and go through `app/repositories/jobs.py`, never
   raw SQL.
6. **Ask before changing another track's files.** Ownership follows the code
   into `main`: `frontend/*` is E's, `docker-compose.yml` is D's,
   `app/api/uploads.py` and `app/services/storage.py` are B's,
   `app/api/jobs.py` / `app/repositories/` / `migrations/` are C's,
   `app/worker/*` and `app/queue.py` are A's.
7. **`main` is protected** — changes are supposed to go through a pull request.

---

## 4. Scope and honest capacity

Sprint 2 already carries FFmpeg **and** crash recovery. [`scaling-notes.md`](scaling-notes.md)
§7 calls that a full sprint on its own, and that assessment stands.

The proposal's Phase 2 (resumable upload, metadata management,
Public/Unlisted/Private visibility, RBAC) is **not** in this plan. Attempting it
alongside FFmpeg and auth would deliver none of them properly. If the module
requires Phase 2 content, drop S2-05 and S2-06 first and say so explicitly in
the sprint record.

### Priority

| Id | Item | Size | State |
|---|---|---|---|
| [S2-01](#s2-01--ffmpeg-transcoding) | FFmpeg transcoding | 2–3 days | **DONE** — deployed, verified transcoding 1920×1080 → 1280×720 in production |
| [S2-02](#s2-02--reaper-and-sweeper) | Reaper + sweeper (covers P2) | 2 days | **DONE** — deployed; crash recovery demonstrated with `docker kill` |
| [S2-04](#s2-04--cicd-pipeline) | CI/CD pipeline | 0.5 day | **DONE** — 5 jobs, required status checks on `main` |
| [S2-03](#s2-03--authorization-p1) | Authorization (P1) | 2–3 days | **Must** — the only thing blocking a real deployment. Mostly C's files. Mechanism decided: [design note](s2-03-auth-design.md) |
| [S2-05](#s2-05--tls) | TLS on 443 | 0.5 day | **DONE** — <https://flickpond.com>, cert expires 2026-12-09, 80 redirects to 443 |
| [S2-06](#s2-06--consolidate-the-minio-clients) | Consolidate MinIO clients | 0.5 day | Should — **do NOT fold into S2-01**, it spans three tracks |
| [S2-07](#s2-07--ensure_bucket-per-upload-p6) | `ensure_bucket` per upload (P6) | 1 hour | Could |
| [S2-08](#s2-08--error-message-hygiene-p9) | Error message hygiene (P9) | 2 hours | Could |

### What the finished items actually left behind

Read these before touching the worker or the reaper:

* **The processing seam held.** `app/worker/tasks.py` was not modified for
  FFmpeg. A future processor implements `ProcessingStep` and changes
  `get_processing_step()`; nothing else.
* **The reaper's orchestration is covered by fakes** (`tests/test_reaper.py`)
  and its data-touching paths by real services
  (`tests/integration/test_reaper_postgres.py`). The integration file
  deliberately does not call `run_once()` — it sweeps the whole table and
  bucket, so running it against a shared stack would destroy other people's
  work.
* **Four traps came out of this work**: T-14, T-15, T-18 and T-19 in
  [`known-traps.md`](known-traps.md). T-18 is BUG-02 from sprint 1 recurring.

### Dependencies

```
S2-04 (CI)     ── done
S2-01 (FFmpeg) ── done
S2-02 (reaper) ── done
S2-03 (auth) ────────► then DELETE the basic auth gate (deploy/auth) and the
                       `include /etc/nginx/app-auth/*.conf;` line in nginx.conf
S2-06 ───────────────► separate and coordinated; spans A, B and C
S2-05 (TLS) ─────────► independent, blocked on a domain name
```

---

## S2-01 — FFmpeg transcoding

**Goal:** replace the copy stand-in with a real transcode to a normalised MP4.

**Why:** it is the sprint's headline deliverable and the reason the async
architecture exists.

### The seam already exists

`app/worker/storage.py` defines the protocol the worker calls:

```python
class ProcessingStep(Protocol):
    def run(self, *, job_id: UUID, source_key: str) -> str:
        """Process the source object and return the resulting output key."""
```

`CopyProcessor` implements it today. **Nothing in `app/worker/tasks.py` should
change.** You are writing a second implementation and switching what
`get_processing_step()` returns.

### Files to touch

| File | Change |
|---|---|
| `Dockerfile` | `apt-get install -y ffmpeg` before the pip install |
| `app/worker/storage.py` | Add `FfmpegProcessor`; point `get_processing_step()` at it |
| `app/config.py` | Add transcode settings (preset, CRF, scale, timeout) |
| `tests/test_worker_ffmpeg.py` | New — unit tests with a fake runner |
| `tests/integration/test_ffmpeg.py` | New — one real transcode, guarded |

### Steps

1. **Install FFmpeg in the image.** The container runs as the non-root
   `flickpond` user (added in sprint 1), so install as root *before* the `USER`
   line. Verify with `docker compose run --rm worker ffmpeg -version`.

2. **Write `FfmpegProcessor`.** It must satisfy `ProcessingStep`. Shape:

   - Check `object_exists(source_key)`; raise `ObjectStoreError` with a readable
     message if missing (this string is shown to users — see contract §US4).
   - Download to a temp file. **Use `tempfile.mkdtemp()`, which lands in `/tmp`
     and is writable by the non-root user.** Do not write into `/app`; it is
     root-owned and read-only to the process by design.
   - Run FFmpeg with an explicit timeout (`subprocess.run(..., timeout=...)`).
     Default to `worker_job_timeout_seconds` (900) minus a margin, so FFmpeg
     dies before RQ kills the job and you keep a readable error.
   - On non-zero exit, raise `ObjectStoreError` (or a new `TranscodeError`) with
     the **last few lines of stderr**, truncated. `readable_error()` in
     `app/worker/tasks.py` already caps messages at 500 chars.
   - Upload the result, return the output key.
   - Clean up the temp directory in a `finally`.

3. **Use `StorageService.download_file` / `upload_file`.** They already exist in
   `app/services/storage.py` and are currently **unused** — they were written
   for exactly this. Note they are `async`; the processing step runs in a thread
   via `asyncio.to_thread`, so either use the synchronous MinIO client directly
   (as `MinioObjectStore` does) or restructure deliberately. Prefer matching
   `MinioObjectStore`'s synchronous style.

4. **Pick one output format** and normalise everything to it. Suggested
   starting point, not a requirement: H.264 + AAC in MP4,
   `-preset veryfast -crf 23`, cap height at 720p, `-movflags +faststart` so the
   file plays before it is fully downloaded. Put these in `app/config.py`, not
   inline.

5. **Update the output key.** `output_key_for()` currently reuses the source
   filename. A transcode changes the container, so the extension must become
   `.mp4`.

### Acceptance criteria

- A real MP4 upload reaches `done` and plays in the browser.
- A file that passes container sniffing but is not decodable (valid `ftyp`
  header, garbage after — see T-02) reaches `failed` with a readable error, not
  a crash and not a hang.
- A job exceeding the timeout reaches `failed` with a timeout message.
- `docker compose up --scale worker=2` still distributes jobs.
- Coverage stays at or above the current level.

### Traps

- **T-02** — sniffing proves the container, not decodability. FFmpeg is what
  finally rejects a fake. Expect previously-accepted test fixtures to start
  failing; that is correct behaviour, so fix the fixtures rather than the code.
- The non-root user cannot write outside `/tmp`.
- FFmpeg writes progress to **stderr**, not stdout, and a non-zero exit with an
  empty stderr is possible — `readable_error()` handles the empty case but check
  the message is actually useful.
- A 100 MB input can produce a large temp footprint. The host has 22 GB free;
  clean up in `finally` or you will fill it across many jobs.

---

## S2-02 — Reaper and sweeper

**Goal:** no job is silently stranded, in any state.

**Why:** two documented limitations close together, and they share a component.
See [`sprint2-backlog.md`](sprint2-backlog.md) P2 for the full analysis and
costing.

### The three failure modes

| Row state | Cause | Today | Fix |
|---|---|---|---|
| stuck `processing` | worker killed mid-job | stays forever | lease timeout → `failed` |
| stuck `queued` | enqueue failed after row insert | stays forever | re-enqueue |
| orphan object | insert failed after upload | invisible forever | delete |

`POST /upload` writes to MinIO, PostgreSQL and Redis in sequence with no
transaction spanning them. Any step can fail after the earlier ones committed.
Nothing unwinds what already succeeded — that is P2.

### Why a sweeper rather than compensating actions

Compensating actions in the request handler need the API to write `status`,
which breaks contract §N4 (worker is the sole writer) and needs a
`queued -> failed` transition that does not exist. They also do not survive a
process being killed between steps. The sweeper is worker-side, changes no
contract, and survives crashes.

### The thing that makes this cheap

`app/queue.py` sets the RQ job id **to the database job id**:

```python
target.enqueue(PROCESS_JOB_TASK, str(job_id), job_id=str(job_id))
```

So the sweeper can ask `rq.job.Job.exists(str(job_id), connection=...)`
directly. No lookup table, no scan, no correlation logic.

### Files to touch

| File | Change |
|---|---|
| `app/worker/reaper.py` | New — the periodic pass |
| `app/repositories/jobs.py` | Add `list_stale(...)`; add a reaper-owned transition |
| `app/models/job.py` + `migrations/` | Optional: a `lease_expires_at` column |
| `docker-compose.yml` | New `reaper` service, or a flag on the worker |
| `app/config.py` | Lease duration, sweep interval, orphan grace period |
| `tests/test_reaper.py` | New |
| `tests/integration/test_reaper_postgres.py` | New, guarded |

### Steps

1. Decide how "stale" is defined. Simplest: `updated_at` older than a
   configurable lease (start at 2× `worker_job_timeout_seconds`). A
   `lease_expires_at` column is cleaner but needs a migration.
2. Add `processing -> failed` as a **reaper-owned** transition in
   `app/repositories/jobs.py`, using the same conditional-update pattern as
   `_transition` so two reapers racing cannot both win. Error message must be
   readable: something like `"worker stopped responding; job was not completed"`.
3. Re-enqueue `queued` rows older than the grace period where
   `Job.exists(id)` is false. **Idempotency matters:** RQ uses the DB id as the
   job id, so a duplicate enqueue is naturally deduplicated — but confirm the
   behaviour rather than assuming it.
4. Orphan objects: list `uploads/` and delete keys with no matching row and an
   age past the grace period. `StorageService` has no delete method yet — add
   one. **Order the check to avoid deleting an in-flight upload:** read the row
   set first, then list objects, and only delete objects older than the grace
   period.
5. Run it on an interval. Simplest is a loop with `asyncio.sleep` in its own
   container; `rq-scheduler` is an alternative but adds a dependency.

### Acceptance criteria

- `docker kill` a worker mid-job; the row reaches `failed` with a readable error
  within the lease period. **This is the sprint 1 `docker kill` demo, now
  passing rather than demonstrating a limitation.**
- A `queued` row with no queue entry gets re-enqueued and completes.
- An orphan object is removed; an in-flight upload is **not**.
- Two reapers running simultaneously do not double-transition a row.

### Traps

- **T-12** — the reaper's integration tests share a database with the live
  stack. Never assert on absolute row counts.
- Deleting an object for a job that is mid-upload is a data-loss bug. The grace
  period is what prevents it — pick it deliberately and test the boundary.
- A reaper that marks jobs failed too eagerly is worse than no reaper. The lease
  must exceed the longest legitimate FFmpeg run, which changes once S2-01 lands.

---

## S2-03 — Authorization (P1)

> **The mechanism is decided:** signed JWTs in an `HttpOnly` cookie. Schema,
> endpoints, cookie attributes, the open operator-view question and the
> teardown checklist are in
> [`s2-03-auth-design.md`](s2-03-auth-design.md). **Read that first** — it
> exists so nobody re-derives decisions already made.

**Goal:** a user sees their own jobs, and only their own.

**Why:** this is the only item that blocks any deployment reachable by someone
outside the team. It is currently mitigated by loopback binding plus an HTTP
basic auth gate — both deployment controls, neither a fix. See
[`sprint2-backlog.md`](sprint2-backlog.md) §3.

**This was not hypothetical.** The deployment ran with `GET /api/jobs` serving
every job in the table, with a working signed download URL for each, to anyone
on the internet who found the address.

### Files to touch

| File | Change |
|---|---|
| `app/models/job.py` + `migrations/versions/` | `owner_id` column, indexed |
| `app/repositories/jobs.py` | Scope `list_jobs`; owner-check `get_job` |
| `app/api/jobs.py`, `app/api/uploads.py` | Resolve caller identity |
| `app/api/deps.py` | New — the identity dependency |
| `docs/contract.md` | Document the auth scheme — **tell the team** |
| `deploy/auth/` | **Delete once this lands**, and remove the nginx include |

### Steps

1. Add `owner_id` with a migration. Existing rows need a value —
   backfill to a sentinel "legacy" owner rather than allowing NULL, so the
   not-null constraint can be enforced from the start.
2. Choose the identity mechanism. The proposal specifies JWT sessions with
   RBAC. Whatever you choose, put it behind a single FastAPI dependency so the
   endpoints stay readable.
3. Scope `list_jobs(session, *, owner_id, limit, offset)`.
4. `get_job` across owners must return **404, not 403** — a 403 confirms the id
   exists and lets someone probe for valid job ids.
5. Decide the operator view. Sprint 1's plan has an operator story ("see all
   jobs and their status") which is exactly the endpoint that must stop being
   public. It needs a role, not deletion. **Decide this early — it changes the
   endpoint's shape, not just its filter.**
6. When this lands: delete `deploy/auth/`, remove the
   `include /etc/nginx/app-auth/*.conf;` line from `nginx.conf`, and update the
   README. Basic auth is a shared password — it tells you nobody uninvited got
   in, not who did what.

### Acceptance criteria

- Two users; each sees only their own jobs in `GET /jobs`.
- `GET /jobs/{id}` for another user's job returns 404.
- Uploading assigns the caller as owner.
- The presigned URL for another user's output is not obtainable through the API.

### Traps

- Pagination (S2-01 sprint 1 work, id P5) already exists. Scope the query, do
  not filter after fetching — filtering post-fetch breaks page sizes.
- **T-12** — the test database is shared.

---

## S2-04 — CI/CD pipeline

**Goal:** every push and PR runs lint and tests automatically.

**Why:** this is a **Phase 1 deliverable from the proposal that was never
built** — "automated build/lint/unit-test/security-check pipeline". Proposal
§5.4 makes it a hard requirement with quality gates, and Phase 4 asks for
"complete CI/CD pipeline evidence".

There is a live gap: `main` is protected and requires pull requests, but with no
CI **no status check runs**. The gate is procedural, not enforced. That is how a
branch with two failing frontend tests reached review.

**Do this first.** It is half a day and protects every later change.

### Why this is cheap here

The hard part of adding CI is deterministic, dependency-free tests. That is
already done, and the suite already splits along exactly the line CI needs:
`RUN_POSTGRES_TESTS` and `RUN_LIVE_TESTS` gate everything that needs services.

### Files to touch

`.github/workflows/ci.yml` — new, and the only file required.

### Steps

1. A `lint-and-unit` job: `ruff check .`, then `pytest -q --cov=app` with the
   existing `fail_under = 80` gate. No services needed.
2. A `frontend` job: `npm ci && npm test` in `frontend/`.
3. An `integration` job using GitHub Actions `services:` for postgres, redis and
   minio, with `RUN_POSTGRES_TESTS=1`.
4. A `build` job: `docker compose build`.
5. Add Trivy image scanning — proposal §5.4 names it explicitly.
6. **Make these required status checks** on `main` in the repository settings,
   otherwise the PR rule still enforces nothing.

### Acceptance criteria

- A PR with a failing test cannot be merged.
- The workflow passes on current `main`.
- Total runtime under ~5 minutes for the unit path.

### Traps

- **T-11** — an ungated integration test will fail in CI. This already happened
  once with `tests/integration/test_storage.py`.
- **T-15** — a CRLF script fails in a Linux runner.
- `ruff format` is **not** clean on two pre-existing files and is not part of
  the project's gate. Run `ruff check`, not `ruff format --check`, or you will
  fail CI on unrelated files.

---

## S2-05 — TLS

**Goal:** serve the app over HTTPS.

**Why:** the basic auth gate currently sends credentials in cleartext — base64
is not encryption. Port 443 is already open in the Alibaba security group
(verified 9 Sep); nothing is listening on it.

### Steps

1. Add certbot, or use the `nginx` + `certbot` companion pattern in compose.
2. A domain name is needed — Let's Encrypt will not issue for a bare IP. If no
   domain is available, this item is blocked; say so rather than leaving it
   silently undone.
3. Redirect 80 → 443 once the certificate is in place.
4. Add HSTS only after HTTPS is confirmed working, and start with a short
   `max-age` — a long one is very hard to undo.

### Traps

- Certbot's HTTP-01 challenge needs `/.well-known/acme-challenge/` reachable
  **without** basic auth. Add a location block exempting it, exactly as
  `/healthz` is exempted (T-07's neighbour in `nginx.conf`).

---

## S2-06 — Consolidate the MinIO clients

**Goal:** one place that builds a MinIO client.

**Why:** there are currently three — `app/services/storage.py` (upload path),
`app/services/output_urls.py` (presigning), `app/worker/storage.py` (worker).
Each has its own `@lru_cache` factory and its own settings read. They can drift.

**Do this inside S2-01**, since you will be in `app/worker/storage.py` anyway.
Doing it separately means touching the same files twice.

### Traps

- `output_urls.py` deliberately uses `MINIO_PUBLIC_ENDPOINT`, not
  `MINIO_ENDPOINT` — the presigned URL must be reachable **by the browser**,
  and the SigV4 signature is computed over that host. Consolidating these two
  into one client will silently break playback. Keep the endpoint distinction.

---

## S2-07 — `ensure_bucket` per upload (P6)

`StorageService.upload_stream()` calls `ensure_bucket()` on every upload, which
issues a `bucket_exists` round trip before every single upload — on a path
budgeted under 1 s (contract N1).

Move it to an application startup hook (FastAPI lifespan) and drop it from the
hot path. One hour. Flat cost, no urgency.

---

## S2-08 — Error message hygiene (P9)

`readable_error()` in `app/worker/tasks.py` writes exception class names into
the user-facing `error` column, and `ObjectStoreError` messages embed object
keys. `GET /jobs/{id}` returns that column verbatim.

Split the user-facing message from the logged diagnostic. Two hours. Do it
before real users see it, not before the demo.

---

## 5. Definition of done for sprint 2

An item is done when **all** of these hold:

- [ ] Code merged to `main` **through a pull request**, with CI green.
- [ ] Unit tests cover the new logic; integration tests carry the
      `RUN_POSTGRES_TESTS` guard.
- [ ] Coverage did not drop below the current level.
- [ ] `ruff check .` passes.
- [ ] Frontend tests pass if any frontend file changed.
- [ ] Demonstrated against the **running stack**, not by inspection.
- [ ] Documentation updated: `contract.md` if the API changed, `README.md` if
      setup changed, `known-traps.md` if you hit something new.
- [ ] The deployed host redeployed from `main` and verified.

## 6. Suggested order

~~1. S2-04 (CI)~~ · ~~2. S2-01 (FFmpeg)~~ · ~~3. S2-02 (reaper)~~ — all done.

What is actually left:

1. **S2-03 (auth)** — the only remaining Must. Largely C's files
   (`models/`, `migrations/`, `repositories/`, `api/jobs.py`), so agree the
   owner and the operator-view decision before anyone writes code. When it
   lands, delete `deploy/auth/` and the nginx include.
2. **S2-06** as a separate, coordinated change — not folded into anything.
3. **S2-07** and **S2-08** as filler.
4. **S2-05 (TLS)** only if a domain appears. Port 443 is already open in the
   security group; the blocker is the certificate, not the network.

## 7. If you are picking this up cold

Start here:

1. Get the stack running (§2) and upload a video through the UI. Watch it go
   `queued -> processing -> done`. Ten minutes, and it makes everything below
   concrete.
2. Read [`contract.md`](contract.md) — 83 lines, and it is the shared boundary.
3. Skim [`known-traps.md`](known-traps.md).
4. Read `app/worker/tasks.py` — 130 lines, and it is the clearest statement of
   how the system actually behaves.
5. Then start on S2-04.
