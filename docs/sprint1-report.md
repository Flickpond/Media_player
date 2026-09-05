# Sprint 1 — Team Record

**Project:** Flickpond — asynchronous video upload and processing platform
**Sprint window:** 4–10 September 2026
**Last updated:** 5 September 2026 · `main` @ `0e85e5c`

A living record of what was built, by whom, what broke, and what evidence exists.
Update it as the sprint continues — see [Revision log](#revision-log) at the bottom.

A rendered version of this document is published at
<https://claude.ai/code/artifact/f64697a4-c1b8-47ab-b931-3f86d6d0196d>
(update it from the same source when this file changes).

---

## 1. Where the project stands

**Verified end to end:** upload → queue → worker → status → playback.

A clone of `main` brings up six services and plays a processed video in the
browser. This was verified by doing exactly that from a clean clone, not by
inspection.

| Measure | Value |
|---|---|
| Tests passing | 99 |
| Python coverage | 99% |
| Frontend coverage (`app.js`) | 100% |
| Upload response, 3 MB file | 95 ms |
| Cold start, six services from wiped volumes | ~25 s |

Against the plan's schedule, Monday's and Tuesday's integration work is finished
three days early. What remains is Thursday's evidence-gathering and write-ups
(section 6).

---

## 2. How the system fits together

Queue for coordination, database for state, object store for bulk data. Each
boundary exists so that piece can scale, fail and be replaced independently.

```text
browser  ──upload──▶  FastAPI  ──▶ MinIO        (source object)
   ▲                     │      ──▶ PostgreSQL  (job row, status queued)
   │                     └──────▶ Redis + RQ    (job id)
   │                                  │
   └────poll 2s──── FastAPI ◀──       ▼
                       ▲          worker ×N
                       └──status──────┘
                                  └──copy──▶ MinIO (output object)
```

The API never touches the transcode and the worker never serves a request. In
Sprint 1 the "transcode" is a server-side object copy standing in for FFmpeg —
deliberately, so the architecture can be proven before the encoding is.

### Job state machine

```text
queued ──▶ processing ──▶ done
                       └─▶ failed   (always with a readable error)
```

One-way transitions, no retries. Claiming a job is a conditional
`UPDATE ... WHERE status = 'queued'`, so when N workers race for the same queue
entry exactly one wins and the losers walk away without touching the row.

After the API's initial insert the worker is the **only** writer to `status`,
`output_key`, `error` and `updated_at`. That single rule is what stops two
components racing on one row, and it is enforced by routing every write through
one shared repository module.

---

## 3. Who built what

Figures from `git log` across all branches, excluding merge commits and
`package-lock.json`.

| Track | Contributor | Owned and delivered | Commits | Lines added |
|---|---|---|--:|--:|
| **A** | Ibrahim Mammadov (@1brahim74) | `app/worker/` (state machine, copy step, db, entrypoint), `app/queue.py`, Compose worker + frontend services, 9 test modules | 10 | 2,493 |
| **C** | David (@ttydw-ch) | `app/api/jobs.py`, `app/repositories/jobs.py`, models, schemas, `database.py`, `output_urls.py`, `migrations/`, Dockerfile, README, `docs/contract.md` | 2 | 1,457 |
| **D** | @JiangYibai666 | `docker-compose.yml` (5 services, health checks, startup gating), `.env.example`, `docs/proposal.md` | 2 | 606 |
| **B** | @zhanj384 | `app/api/uploads.py` (POST /upload), `app/services/storage.py` (MinIO client), `tests/integration/test_storage.py` | 4 | 220 |
| **E** | @Dilute-l | `frontend/app.js` (upload, poll, play), `index.html`, `style.css`, CORS middleware in `app/main.py` | 1 | 212 |

### Reading these numbers honestly

Line counts measure volume, not value. Track D's 606 lines are the Compose
foundation every other track depends on, and they landed first. Track E's 212
lines are the only part of the system a user actually sees. Track B's 220 lines
are the entry point for the whole pipeline.

Of track A's 2,493 lines, **1,565 are test code** and only 390 are application
code — the remainder is documentation and Compose configuration. A large share
of that testing covers other tracks' components, added during the 5 September
integration session.

---

## 4. Bug log

Symptom / cause / fix, per the sprint plan's requirement for `docs/bugs.md`.

### BUG-01 — Uploads were never enqueued · **critical** · fixed `6d6925d`

- **Symptom.** None observed yet — nothing had been integrated, so nobody had
  seen it. Every uploaded job would have sat at `queued` forever with no worker
  running it. The UI would have shown a permanent "Queued…" spinner.
- **Cause.** `POST /upload` stored the object in MinIO and inserted the database
  row, then returned. It never enqueued. The enqueue seam `app/queue.py` did not
  exist on track B's branch at all, because that branch was cut before track A's
  work existed.
- **Fix.** Merged track A into track B to bring the queue module across, then
  three lines: import `enqueue_job` and call it after `create_job`, via
  `run_in_threadpool` because the Redis client blocks and the upload must stay
  under one second.
- **Why enqueue last.** Reverse the order and a fast worker can dequeue a job id
  whose row has not been committed yet, then fail to find it.
- **Cost.** ~20 minutes to find, 10 to fix. Had it survived to integration day it
  would have presented as "the worker is broken" and cost hours aimed at the
  wrong component.

### BUG-02 — Worker silently ignored `--log-level` · **moderate** · fixed `05bed8c`

- **Symptom.** Passing `--log-level warning` changed nothing. No error, no
  warning — the flag was inert.
- **Cause.** `logging.basicConfig()` is a no-op once the root logger already has
  handlers, and RQ installs its own before the worker's setup code runs.
- **Fix.** `force=True`, which tears down existing handlers and reconfigures.
- **How found.** By writing the first test for the worker entrypoint, which had
  been at **0% coverage**. The module looked obviously correct, and was not.

### BUG-03 — A clone of `main` had no way to show the app · **moderate** · fixed `0e85e5c`

- **Symptom.** `docker compose up` produced five healthy backend services and no
  user interface. Opening `frontend/index.html` from disk failed too: every
  request was blocked and the page did nothing.
- **Cause.** Two compounding gaps. Nothing in Compose served the static
  frontend, and a page opened from the filesystem sends a `null` origin, which
  the API's CORS allowlist rejects.
- **Fix.** nginx service on port 3000 — the origin `app/main.py` already allows.
  Port and allowlist are coupled, so the Compose file says so in a comment. The
  README, which predated the upload endpoint, the worker and the frontend, was
  corrected in the same commit.

### ENV-01 — "The page is completely black" · **environment, not a defect**

- **Symptom.** The frontend, served by hand from `python -m http.server`,
  rendered as a blank black page.
- **Cause.** Not the application. The hand-started server was resetting
  connections mid-load (its log shows repeated `GET /` with no CSS or JS
  follow-up — the signature of a blank load), and it was bound to
  `127.0.0.1:3000`, shadowing the container's `0.0.0.0:3000` so the replacement
  never received traffic.
- **Also present.** AdGuard intercepts localhost HTTP on this machine and
  injects `<script src="//local.adguard.org…">` into served pages —
  `index.html` is 759 bytes on disk but 1,248 bytes arrive. It did not break
  rendering, but it is the first thing to suspect for any browser oddity that
  cannot be reproduced elsewhere.
- **Resolution.** Superseded by BUG-03's nginx service.

---

## 5. Non-functional requirements: evidence

| ID | Requirement | Evidence | Status |
|---|---|---|---|
| N1 | Upload returns <1 s regardless of file size | 3 MB multipart upload returned `202` in **95 ms** | met |
| N2 | Throughput at one worker versus two | `--scale worker=2`, 8 jobs of 4 MB, split **5 / 4** across replicas | met |
| N3 | Failures reach `failed` with a cause, never hang | Missing source object produced `source object missing from storage: uploads/…`; zero rows left non-terminal | **partial** |
| N4 | Exactly one component writes status after creation | All worker writes route through the shared repository; no raw SQL in `app/worker/`; asserted in tests | met |
| N5 | Workers stateless — any worker takes any job | No job affinity; conditional claim proven under a two-worker race, in tests and at runtime | met |
| N6 | Runs identically on all five machines | Verified on one machine from a clean clone; four teammates have not reported | **unproven** |
| N8 | 100 MB file size limit | Tested *at* the limit and one byte over; oversized uploads store nothing and enqueue nothing | met |
| N9 | Every transition logged with the job id | `job 3d5b…: queued -> processing (source_key=…)` in worker logs | met |

**N3 is partial deliberately.** Exception handling is proven; the `docker kill`
fault injection is not yet done, and it will expose a real limitation rather
than a clean pass — see section 6.

### Test suite

| Suite | Tests | Coverage | Requires |
|---|--:|--:|---|
| Python unit | 72 | — | nothing |
| Python integration | 8 | — | `RUN_POSTGRES_TESTS=1` + live PostgreSQL |
| **Python total** | **80** | **99%** | — |
| Frontend unit | 16 | 100% | Node 20+ |
| Frontend live | 3 | — | `RUN_LIVE_TESTS=1` + running stack |

One test is skipped by design: the forking-worker assertion cannot pass on
Windows, which has no `fork`. Verified inside the Linux container instead. The
two uncovered Python lines are a `__main__` guard and one branch in the
repository module.

---

## 6. Known limitations

Scoped out on purpose. Naming them is part of the deliverable — an unstated
limitation reads as an oversight.

- **A worker killed mid-job strands its row.** No reaper, no lease timeout, so
  the job stays at `processing` with nobody left to write to it. Exceptions are
  handled; sudden death is not. This is the honest result Thursday's
  `docker kill` demo will show, and the sprint 2 item it justifies.
- **No retries.** A failed job is terminal. Retries without idempotency
  guarantees would hide bugs rather than survive them.
- **Processing is a file copy, not a transcode.** Proves the architecture before
  the encoding. Sprint 2 replaces one class behind the `ProcessingStep` protocol.
- **No authentication, quotas, or format selection.** Out of module scope.
- **Single-node only.** Scaling shown with Compose replicas on one host — a
  documented, accepted constraint.

---

## 7. What is left

Thursday's items, none started:

- [ ] **Fault injection.** `docker kill` a worker mid-job, observe, document.
      Expect the stranded-row limitation above — that is the finding, not a failure.
- [ ] **The design note.** Owned by track A: idempotency, the write-ownership
      rule, what happens when a finished job is delivered twice, and what is
      explicitly out of scope.
- [ ] **N6 confirmation.** Four teammates need to run a clean clone and report.

### Needs a decision, not just work

- [ ] **Two commits on `main` will show as anonymous.** Authored as
      `ibrahim.22@intl.zju.edu.cn`, which is not linked to the GitHub account —
      exactly what the plan's §6 warns about. Safest fix: add that address as a
      secondary email on the GitHub account, which attributes them
      retroactively. Rewriting published history is not worth it. Separately,
      set `git config --global user.email` to the account address so it stops
      recurring.
- [ ] **The module code is inconsistent.** The plan says `SWE5001`; the README
      says `SWE5006`. One is wrong and it appears on a submitted artefact.
- [ ] **Two files fail `ruff format --check`:** `app/services/storage.py` and
      `tests/integration/test_storage.py`, both track B's and both pre-existing.
      Left untouched deliberately rather than creating merge noise in someone
      else's work.
- [ ] **Track B's branch now contains track A's worker**, from the merge that
      carried the queue module across. B should know before pushing again.

---

## 8. Running it

```bash
# clone and configure
git clone https://github.com/Flickpond/Media_player.git
cd Media_player && cp .env.example .env

# bring up all six services
docker compose up --build -d

# open the app - the port matters, see below
http://localhost:3000

# python tests, including the 8 that need real PostgreSQL
RUN_POSTGRES_TESTS=1 pytest --cov=app

# frontend tests
cd frontend && npm install && npm test
RUN_LIVE_TESTS=1 npx vitest run app.live.test.js

# the two-worker scaling demonstration
docker compose up -d --scale worker=2
docker compose logs -f worker
```

**Use port 3000 exactly.** The API's CORS allowlist accepts only
`localhost:3000` and `127.0.0.1:3000`. Opening `frontend/index.html` from the
filesystem will not work — the browser sends a `null` origin and every request
is blocked. Changing the Compose port means changing `allow_origins` in
`app/main.py` to match.

---

## Revision log

Add a row whenever this document is updated, so the team can see what changed
and when.

| Date | Author | What changed |
|---|---|---|
| 5 Sep 2026 | @1brahim74 | Initial record: contributions, BUG-01 to BUG-03, ENV-01, NFR evidence, limitations, outstanding items. |

### How to update this

- **New contributions** — refresh section 3 with
  `git log --all --no-merges --format='@%an' --numstat -- . ':(exclude)*package-lock.json'`
- **New bugs** — add a `BUG-0n` entry to section 4 in the same
  symptom / cause / fix form. Keep the numbering sequential and never reuse an id.
- **NFR evidence** — update section 5 as items move from unproven to met, and
  paste the actual command output as evidence rather than describing it.
- **Tick items off** section 7 as they land; move anything that turns out to be
  permanent into section 6 instead.
