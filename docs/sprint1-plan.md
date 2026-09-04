# Sprint 1 — Team Plan & Contract
**Project:** Async video upload/transcode platform — SWE5001
**Sprint 1 goal:** Upload → queue → worker → status → play. No real transcoding — worker does a copy job as a stand-in for FFmpeg.
**Sprint window:** 4 September – 10 September 2026

---

## 1. User Stories (Sprint 1 scope)

| #                | As a...            | I want to...                                        | So that...                               | Owner          |
| ---------------- | ------------------ | --------------------------------------------------- | ---------------------------------------- | -------------- |
| US1              | User (Maya/Daniel) | upload a video file                                 | it starts processing without me waiting  | B              |
| US2              | User               | get a job ID immediately after upload               | I can check on it later                  | B              |
| US3              | User               | see my job's status (queued/processing/done/failed) | I know it's not broken, no dead spinner  | C              |
| US4              | User               | get a readable error if it fails                    | I'm not left guessing                    | Worker (A) + C |
| US5              | User               | play the result once status is `done`               | the pipeline actually delivers something | E              |
| Operator (Priya) | operator           | see all jobs and their status                       | I can spot stuck jobs                    | C              |
| Platform         | system             | run 2 worker replicas sharing the queue             | horizontal scaling is demonstrated (N2)  | A + D          |

**Explicitly NOT in sprint 1:** real FFmpeg transcoding, retries, auth, format selection, MKV.

---

## 2. Ownership — who owns what, done-when

| Person | Owns                                   | Done when                                                                                                    |
| ------ | -------------------------------------- | ------------------------------------------------------------------------------------------------------------ |
| **A**  | Worker + state machine + integration   | Worker consumes from Redis, transitions correctly, writes `failed` with readable error, 2 workers share load |
| **B**  | Upload endpoint + storage client       | `POST /upload` stores file in MinIO, inserts DB row, enqueues job, returns `job_id` in <1s                   |
| **C**  | Status endpoints + DB layer            | Schema + migration + repository module; both GET endpoints return real data                                  |
| **D**  | Compose, Redis, Postgres, MinIO wiring | `docker compose up` gives 5 healthy services on **every** teammate's machine                                 |
| **E**  | Frontend                               | Upload form, polls every 2s, renders player on `done`, shows error on `failed`                               |

D is highest-risk (blocks everyone) — check in on D's progress Saturday, not Sunday or later.

---

## 3. The Contract — commit this to `docs/contract.md` before anyone codes a feature

### 3.1 Jobs table (C builds this; B, A code against it)

| Column       | Type        | Notes                                                                             |
| ------------ | ----------- | --------------------------------------------------------------------------------- |
| `id`         | uuid        | primary key                                                                       |
| `filename`   | text        | original name                                                                     |
| `status`     | text        | exactly one of: `queued`, `processing`, `done`, `failed` — lowercase, no variants |
| `source_key` | text        | MinIO object key (input)                                                          |
| `output_key` | text        | MinIO object key (output); null until `done`                                      |
| `error`      | text        | null unless `failed`                                                              |
| `created_at` | timestamptz |                                                                                   |
| `updated_at` | timestamptz | updated on every status transition                                                |

Index on `status` and `created_at`.

**Write ownership rule (non-negotiable):** API sets `queued` at insert — that's the API's only write. After insert, **the worker is the sole writer** to `status`, `output_key`, `error`, `updated_at`. Prevents two components racing on the same row.

### 3.2 API endpoints (B and C build against this; E consumes it)

```
POST /upload
  → 202 { "job_id": "<uuid>" }        (202, not 200 — "received, not finished")
  → 400 { "error": "file too large" }

GET /jobs/{id}
  → 200 { id, filename, status, output_url?, error? }
  → 404 { "error": "not found" }

GET /jobs
  → 200 [ ... same shape as above ... ]
```

**Important distinction:** the DB stores `output_key` (MinIO object path). The API returns `output_url` (something a browser can actually fetch). Converting key → URL is the API's job (presigned URL) — B/C, don't leak raw keys to the frontend.

**Upload constraint:** `POST /upload` must never read file contents into the request handler. Store to MinIO → insert row → enqueue → return. If it takes >1s, something's wrong.

### 3.3 File size limit
**100MB**, decided. Enforce it, test at the limit (N8).

### 3.4 State machine

```
        enqueue          worker picks up       success
  ─────────────►  queued  ─────────────►  processing  ───────►  done
                                               │
                                               │ exception
                                               ▼
                                            failed
```
One-way transitions only. No retries in sprint 1. `failed` always carries a readable `error` string — never a silent hang in `processing`.

---

## 4. What each person needs to share with the group (integration parameters)

This is the "so we don't discover mismatches on Wednesday" list. Post these in the team channel as soon as each is ready — don't wait for standup.

**B (Upload endpoint) shares:**
- Exact MinIO bucket name and key naming convention for `source_key` (e.g. `uploads/{job_id}/{filename}`)
- Redis queue name the job is enqueued to (must match what A's worker listens on)
- Exact JSON shape actually returned by `/upload` (confirm it matches §3.2 or flag the diff)

**C (Status endpoints + DB) shares:**
- Migration script location + how to run it
- Repository module interface (function signatures others might call)
- Confirmation the schema in §3.1 was implemented as-is, or documents any deviation

**D (Infra/Compose) shares:**
- Final `docker-compose.yml` service names, ports, and env var names (everyone else's code needs to match these exactly — e.g. `REDIS_HOST`, `MINIO_ENDPOINT`, `POSTGRES_DSN`)
- Confirmation Compose runs clean on all 5 machines (tracked daily)
- `.env.example` file with required variable names (no real secrets in git)

**A (Worker) shares:**
- Redis queue name I'm consuming from (must match B's enqueue target)
- Exact function signature workers expect for a job payload
- Confirmation worker only writes `status`/`output_key`/`error`/`updated_at`, never touches other columns

**E (Frontend) shares:**
- Polling interval used (2s, per plan) — flag if this needs to change for demo purposes
- Confirms it reads `output_url`, not `output_key`, from the status response

---

## 5. Day-by-day

| Date           | Day                     | Focus                                                                                                                                                                                                                                  |
| -------------- | ----------------------- | -------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| **Fri 4 Sep**  | Kickoff                 | Meeting (90 min). Commit `docs/contract.md`. Repo + branch convention. D starts the Compose skeleton (5 empty services).                                                                                                               |
| **Sat 5 Sep**  | Seams (1/2)             | D finishes Compose skeleton, confirms all 5 machines boot clean. A/C/B/E start isolated work in parallel (see §5b).                                                                                                                    |
| **Sun 6 Sep**  | Seams (2/2)             | Finish isolation work: worker prints a fake job id, migration runs clean, storage client pushes a test file, frontend renders mock data.                                                                                               |
| **Mon 7 Sep**  | Connect (1/2)           | C's schema goes live on D's Postgres. B wires upload → real MinIO + real DB insert. C wires the two GET endpoints to live data.                                                                                                        |
| **Tue 8 Sep**  | Connect (2/2)           | A's worker consumes B's real enqueued jobs, flips `queued → processing → done` for real. E swaps frontend from mocked data to the real endpoints.                                                                                      |
| **Wed 9 Sep**  | Integration day         | Full path from `docker compose down -v`, clean state. Budget 4+ hours — Tuesday's assumptions will break somewhere.                                                                                                                    |
| **Thu 10 Sep** | Failure handling + demo | Worker catches exceptions → `failed` + message. `docker kill` a worker mid-job, observe, document. `--scale worker=2`, upload 3 files, screenshot distribution. Write the design note. Demo from clean state. **Sprint 1 ends today.** |

---

## 5b. Build Workflow — who writes what, in what order, what unblocks what

Read each day as **tracks running in parallel**, with dependency notes showing what a track needs from another track before it can start. If a dependency isn't met, that track waits — don't start it early on stubbed/fake data unless noted.

### Fri 4 Sep — Kickoff (no code, no parallel tracks yet)
- Whole team: agree contract, commit `docs/contract.md`, agree branch convention.
- **D starts:** Compose skeleton — 5 services (API, worker, Redis, Postgres, MinIO) that boot and do nothing. This is the single dependency the rest of the sprint sits on top of.
- *Nothing else can meaningfully start until D's skeleton exists.*

### Sat 5 Sep — Seams, part 1 (parallel tracks — each needs only D's skeleton, not each other)

| Track          | Task                                                                                    | Depends on                                                               |
| -------------- | --------------------------------------------------------------------------------------- | ------------------------------------------------------------------------ |
| **D**          | Finish Compose skeleton, confirm all 5 machines boot clean                              | — (foundation)                                                           |
| **A (worker)** | Start worker that pulls a *hardcoded* job id from Redis and prints it                   | D's Redis container running                                              |
| **C**          | Start `jobs` schema + migration script                                                  | Can draft with no dependency; needs D's Postgres only to actually run it |
| **B**          | Start MinIO storage client (put/get object functions), no DB or Redis wiring yet        | D's MinIO container running                                              |
| **E**          | Scaffold frontend: upload form UI, polling loop skeleton, against fake/mocked responses | Contract doc only                                                        |

### Sun 6 Sep — Seams, part 2 (finish and verify in isolation)

| Track | Task                                                                                 |
| ----- | ------------------------------------------------------------------------------------ |
| **A** | Worker reliably prints the hardcoded job id every run — confirm it before moving on  |
| **C** | Migration applies clean from a fresh Postgres container; schema matches §3.1 exactly |
| **B** | Storage client reliably pushes/pulls a test file to/from MinIO                       |
| **E** | Frontend renders end-to-end against mocked data — form, spinner, fake "done" state   |

→ **End of Sunday:** every piece works alone. Nobody has touched anybody else's code yet — that's the point of "seams."

### Mon 7 Sep — Connect, part 1 (real dependency chain begins)

- **Morning (parallel):** B wires upload → real MinIO + real DB insert; C wires the two GET endpoints against the now-live schema. These run side by side — B writing rows, C reading rows, same table, no blocking between them yet.
- **Afternoon:** confirm B's insert and C's read agree on the same live data before calling the day done.

### Tue 8 Sep — Connect, part 2 (chain closes)

```
B's live upload (Mon)
      │
      └──► A (worker consumes B's real enqueued jobs, writes status back via C's schema)
                │
                └──► E (frontend swaps mock data for real B + C endpoints)
```

- **A is blocked first thing Tuesday until B's enqueue path is confirmed working** — that's the morning's priority.
- Once A's worker is flipping `queued → processing → done` for real, verify C's GET endpoints reflect it.
- **E swaps to real endpoints last** — blocked until both B and C are confirmed live, so this naturally lands end of Tuesday.

### Wed 9 Sep — Integration day (not parallel — this is a single shared track)
- Whole team, `docker compose down -v` → `up` from a clean state, full path end to end.
- Fix whatever Tuesday's assumptions got wrong. Whoever owns the broken piece fixes it — this is the day component boundaries get tested for real.

### Thu 10 Sep — Failure handling + demo (parallel again, low mutual dependency)

| Track         | Task                                                                           | Depends on                                 |
| ------------- | ------------------------------------------------------------------------------ | ------------------------------------------ |
| **A**         | Worker catches exceptions → writes `failed` + readable error                   | Tuesday's worker already stable            |
| **D**         | `--scale worker=2`, run the throughput/distribution test, capture screenshots  | A's worker stable enough to run 2 replicas |
| **B / C**     | Bug-fix anything integration day surfaced in their components                  | Wed's integration findings                 |
| **E**         | Frontend renders the error state on `failed`, confirms polling stops correctly | A's `failed` status working                |
| **A (again)** | Write the design note (idempotency, write-ownership rule, what's out of scope) | Everything above, roughly last             |

D's scaling test and A's failure-handling can run in parallel most of the day; E's error-state work trails slightly behind A's `failed` status landing.

---

## 5c. What we'll have in hand at end of Sprint 1 (Thu 10 Sep)

**Working system:**
- End-to-end pipeline running from a clean `docker compose up`: upload a file → job queued → worker picks it up → status flips `queued → processing → done` → file playable/downloadable
- "Processing" is a copy job, not real transcoding — that's by design, sprint 2 swaps in FFmpeg as a change to one function
- 2 worker replicas sharing the queue, with a screenshot/log showing jobs distributed across both (N2 evidence)
- A `docker kill` mid-job demonstrated and documented — job reaches `failed` with a readable error, no silent hang (N3 evidence)

**Code, committed and reviewed via PR:**
- `docs/contract.md` — schema + API contract, followed by all 5 people
- Upload endpoint (B), status endpoints + DB layer (C), worker + state machine (A), Compose/infra (D), frontend (E) — each demoed working against the real system, not mocks

**Documentation:**
- Design note covering: the write-ownership rule (worker-only writes after insert), what happens if a job is already `done` when picked up twice, and what's explicitly out of scope this sprint (auth, retries, real transcoding)
- `docs/bugs.md` started — anything that cost more than an hour, with symptom/cause/fix/time lost

**What we will NOT have yet** (by design, don't let this read as incomplete):
- Real FFmpeg transcoding — sprint 2
- Retry policy or job recovery after a worker crash mid-job — sprint 2
- Format/resolution selection — sprint 3
- Any auth, quotas, or cloud deployment — out of scope for the whole module

**What this proves for the report/interview, specifically:**
- O1 (decouple slow work from request path) — upload stays <1s regardless of file size
- O2 (horizontal scaling) — 2-worker throughput evidence exists
- O3 (explicit failure handling) — readable errors, no silent hangs, demonstrated live
- O4 (storage/compute separation) — stateless workers, independent MinIO, proven by killing and restarting a worker mid-queue

---

## 6. Git conventions
- Branch naming: `<name>/<short-task>` (e.g. `ibrahim/worker-state-machine`)
- PRs required, reviewed before merge to `main`
- **Before leaving Friday's kickoff meeting: everyone confirms their local `git config user.email` matches their GitHub account email** — mismatched commits show as anonymous and vanish from contribution history.

---

## 7. Non-functional requirements this sprint proves

| ID  | Requirement                                        | Verified by                 |
| --- | -------------------------------------------------- | --------------------------- |
| N1  | Upload returns <1s regardless of file size         | Timed test, large file      |
| N2  | Throughput at 1 vs 2 workers (not 1/2/4)           | Load test, Thu 10 Sep       |
| N3  | Failures reach `failed` with a cause, never hang   | Fault injection, Thu 10 Sep |
| N4  | Exactly one component writes status after creation | Code review + this doc      |
| N5  | Workers stateless — any worker takes any job       | Kill and restart mid-queue  |
| N6  | Runs identically on all 5 machines                 | Verified Fri–Sun            |
| N8  | 100MB file size limit                              | Test at the limit           |
| N9  | Every transition logged with job id                | Log inspection              |

---

## 8. Tech Stack — What We Use and Why

Every piece exists to do one job. The reason there are five separate services instead of one monolith is that using one tool for all of them is exactly where the architecture stops demonstrating anything — the whole point of this module is showing you understood *why* each boundary exists, not just that the demo runs.

### FastAPI — the API layer
**Usage:** Handles `POST /upload` and the two `GET /jobs` endpoints. Never touches file bytes, never runs the transcode — its only job is: store to MinIO → insert DB row → enqueue → return.
**Why:** Async-native, so the upload handler isn't blocked waiting on I/O; auto-generated OpenAPI docs (`/docs`) give B and C a live, browsable contract for free, which matters when 5 people are integrating in parallel.
**Why not Flask/Django:** Both work, but neither gives async handling or auto docs out of the box — you'd hand-roll what FastAPI does natively, for no architectural benefit the module is grading.

### Redis + RQ — the queue
**Usage:** API enqueues a job id after insert; A's worker(s) pull from the same queue, so any of the N workers can pick up any job — this is what makes horizontal scaling (N2, N5) actually true rather than assumed.
**Why:** Redis is in-memory, so enqueue/dequeue is sub-millisecond — it never becomes the bottleneck. RQ gives the worker consume-loop, retry hooks, and job registry for free instead of hand-writing a polling loop.
**Why not Postgres as the queue:** Technically possible (`SELECT ... FOR UPDATE SKIP LOCKED`), but you'd be rebuilding what RQ already gives you, and every poll hits the database that's also serving status reads — more work, worse separation of concerns.
**Why not RabbitMQ or Kafka:** Both are more capable at this, and both cost a week of learning the team doesn't have before Wednesday. Kafka specifically is built for replayable event streams, not "give this job to exactly one worker" — wrong tool even ignoring the learning curve.

### PostgreSQL — job metadata
**Usage:** Holds the `jobs` table (§3.1) — the durable source of truth for status, timestamps, and errors. C's status endpoints read from here; A's worker is the only writer after insert.
**Why:** This is relational, queryable, transactional state — "which jobs have been stuck in `processing` for over an hour" is one line of SQL and painful to answer if that state lived anywhere else. It also survives restarts, which the queue is not designed to guarantee.
**Why not keep status in Redis instead of a separate DB:** Redis is memory-first — restart the container and job history is gone. It's also awkward to query relationally; you'd be reimplementing a database inside a cache.

### MinIO — object storage
**Usage:** Stores the uploaded video (`source_key`) and, once processed, the output (`output_key`). Any worker can read/write any job's files — no worker owns a specific machine's disk.
**Why:** It speaks the S3 API, so the exact same client code works against real AWS S3 later with only a config/endpoint change — you get production-shaped storage semantics without an AWS account or bill during the module.
**Why not the container filesystem:** Then only the worker container that received the upload can see the file — workers stop being interchangeable, which directly breaks N5 (statelessness) and with it the whole scaling story.
**Why not Postgres BLOBs:** Databases handle large binaries badly — bloated backups, memory pressure on every read, slower writes. Object storage exists specifically to take this weight off the database.

### Docker Compose — orchestration
**Usage:** One `docker-compose.yml` brings up all 5 services (API, worker, Redis, Postgres, MinIO) identically, satisfying N6 — "runs identically on all 5 machines" — with one command, both on a laptop and on the DigitalOcean droplet.
**Why:** The module explicitly keeps cloud deployment and orchestration complexity out of scope (Part 4 of the briefing) — Kubernetes would be solving a problem this project doesn't have yet. Compose is the right-sized tool for "single-node deployment, scaling shown via replicas on one host" (a documented, accepted limitation, not a shortfall).
**Where Kubernetes fits instead:** the December fork, after the module deadline and the CKA exam — workers become a Deployment with HPA on queue depth, Redis a StatefulSet, storage a PVC. That's a deliberately separate, later decision, not something sprint 1 needs.

### DigitalOcean Droplet — hosting
**Usage:** A single VM (4GB RAM / 2 vCPU to start) running the same Compose stack as everyone's laptop — `git pull`, `docker compose up -d`, done. Only the API port is exposed publicly; Redis/Postgres/MinIO stay on the internal Compose network.
**Why:** Because the deployment target is "a server," not "a cloud platform" — a Droplet is a plain VM, so the exact same Compose file that runs locally runs there with zero rewriting. That's the whole value: no IAM policies, no billing account complexity, no service that only exists in one cloud's dashboard.
**Why not AWS EC2/ECS for this module:** Would work, but adds IAM role design, security-group configuration, and a shared credential set four teammates would need — real setup cost for a requirement the module doesn't grade. AWS becomes worth it specifically for the December portfolio fork, where showing IAM least-privilege and managed services *is* part of the story.
**Sizing note:** 2 vCPU/4GB covers Postgres + Redis + MinIO + 2 workers running real FFmpeg jobs (each worker can spike 1-2GB during an active transcode). Resize up only if `docker stats` shows pressure during Thursday's integration run — cheap to adjust later, no need to over-provision now.

### The pattern to say out loud in the design note
**Queue for coordination, database for state, object store for bulk data.** That's the whole reasoning in one sentence, and it's the direct answer to "why three separate services instead of one" if an assessor or interviewer asks — each one can now scale, fail, and be replaced independently of the other two.
