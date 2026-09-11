# Flickpond

Flickpond is a practice-module team project: an asynchronous video upload and processing platform. Sprint 1 proved the request path end to end with a copy job standing in for transcoding; **sprint 2 replaced it with real FFmpeg**, added authentication and per-user ownership, a reaper that recovers jobs abandoned by a crashed worker, CI as a required check on `main`, and TLS.

It is deployed at **<https://flickpond.com>**.

```text
browser -> nginx -> FastAPI -> MinIO + PostgreSQL + Redis queue
                                          |
                                          v
                                  one of N workers (FFmpeg)
                                          |
                                          v
                       queued -> processing -> done | failed
                                          |
                                          v
                             browser polls the status API

           reaper: recovers rows a dead worker left in `processing`
```

### Where to read next

| File | What it is |
| --- | --- |
| [`docs/known-traps.md`](docs/known-traps.md) | **Read this before changing anything.** 23 traps already hit here, most of which fail silently. |
| [`docs/contract.md`](docs/contract.md) | The shared schema and API boundary. Changing it means telling the team. |
| [`docs/sprint2-plan.md`](docs/sprint2-plan.md) | Sprint 2: all eight items, what each decided, and why. |
| [`docs/sprint2-backlog.md`](docs/sprint2-backlog.md) | The sprint 1 review's findings, P1-P9. All closed; kept for the reasoning. |
| [`docs/s2-03-auth-design.md`](docs/s2-03-auth-design.md) | Why authentication is shaped the way it is. |
| [`docs/scaling-notes.md`](docs/scaling-notes.md) | What serving 50 concurrent users would take. |
| [`docs/sprint1-report.md`](docs/sprint1-report.md) | What sprint 1 built, by whom, and what broke. |
| [`docs/sprint1-plan.md`](docs/sprint1-plan.md), [`docs/proposal.md`](docs/proposal.md) | The original plan and module proposal. |
| [`CLAUDE.md`](CLAUDE.md) | Working notes: commands, ownership, conventions. |

## Architecture

| Component | Responsibility | Default local address |
| --- | --- | --- |
| nginx (frontend) | Serves the UI; proxies `/api/` and `/videos/`. The only public port. | `http://localhost:3000` (deploy: `:80`) |
| FastAPI | Upload and job-status HTTP API | `127.0.0.1:8000` (loopback only) |
| PostgreSQL | Durable job metadata and processing state | `127.0.0.1:5432` |
| Redis + RQ | Delivery of job IDs to workers | `127.0.0.1:6379` |
| MinIO | Original and processed video objects | API `127.0.0.1:9000`, console `127.0.0.1:9001` (both loopback only) |
| Worker | FFmpeg transcode to 720p MP4, one-way state transitions | Internal Compose service |
| Reaper | Fails rows a dead worker abandoned; sweeps orphaned objects | Internal Compose service |

The queue coordinates work, PostgreSQL records state, and MinIO stores the video bytes. Workers remain stateless, so any worker replica can process any queued job.

### Port policy

**nginx is the only service published beyond loopback.** The API, MinIO's S3 API
and console, PostgreSQL and Redis are pinned to `127.0.0.1` in
`docker-compose.yml` and that is deliberately not configurable.

The API enforces per-user ownership (S2-03), but the datastores behind it do
not: anything that reaches PostgreSQL or MinIO directly reads every user's data.
That is what the loopback binding is for, and why it stays even though the
application now authenticates its own callers.

Reach an internal service from another machine with a tunnel, not a published
port:

```bash
ssh -L 5432:127.0.0.1:5432 user@host    # psql against localhost:5432
ssh -L 9001:127.0.0.1:9001 user@host    # MinIO console on localhost:9001
```

A deployment anyone else can reach sets `FRONTEND_BIND=0.0.0.0` and
`FRONTEND_PORT=80`. The app authenticates its own callers, so no separate gate
is needed — see [`docs/s2-03-auth-design.md`](docs/s2-03-auth-design.md).

Note that `ufw` will not save you here: Docker publishes ports through its own
iptables chain and bypasses ufw entirely, so a host that believes it is
firewalled is not ([T-06](docs/known-traps.md#t-06)).

## Repository layout

```text
Media_player/
|-- app/
|   |-- api/jobs.py               # GET /jobs and GET /jobs/{id}
|   |-- api/uploads.py            # POST /upload
|   |-- models/job.py             # SQLAlchemy Job model and state names
|   |-- repositories/jobs.py      # Shared DB functions for API and worker
|   |-- schemas/job.py            # Public response schemas
|   |-- services/output_urls.py   # Browser-accessible MinIO signed URLs
|   |-- services/media_type.py    # Container sniffing: what an upload actually is
|   |-- services/storage.py       # Upload-path object storage
|   |-- services/minio_client.py  # The only place a MinIO client is constructed
|   |-- services/security.py      # Password hashing and JWT issue/verify
|   |-- api/auth.py               # register / login / logout / me
|   |-- worker/                   # RQ worker, state machine, FFmpeg step, reaper
|   |-- queue.py                  # Shared enqueue/consume seam
|   |-- config.py                 # Environment configuration
|   |-- database.py               # Async SQLAlchemy engine and sessions
|   `-- main.py                   # FastAPI application and /health
|-- frontend/                     # Static UI: upload, poll, play
|-- migrations/
|   `-- versions/                 # Alembic database revisions
|-- tests/
|   |-- integration/              # Tests using the real Compose PostgreSQL
|   `-- test_*.py                 # API and service unit tests
|-- docs/
|   |-- contract.md               # Shared schema and API boundary
|   |-- sprint1-report.md         # Sprint 1 record: contributions, bugs, evidence
|   |-- scaling-notes.md          # Capacity analysis
|   |-- sprint2-backlog.md        # The sprint 1 review's findings, P1-P9
|   |-- sprint2-plan.md           # Sprint 2: what was built, in what order
|   |-- s2-03-auth-design.md      # Why auth is shaped the way it is
|   |-- known-traps.md            # Traps already hit here -- read before coding
|   |-- a-worker.md               # Worker and state machine notes
|   |-- proposal.md               # Full module proposal
|   `-- sprint1-plan.md           # Sprint 1 plan
|-- Dockerfile                    # Python 3.12 API image
|-- docker-compose.yml            # Frontend, API, worker, reaper, Redis, PostgreSQL, MinIO
|-- deploy/                       # nginx config, TLS, certbot renewal
|-- alembic.ini                   # Migration configuration
|-- pyproject.toml                # Runtime and development dependencies
`-- .env.example                  # Safe local configuration template
```

## Prerequisites

- Windows 11 with WSL 2 and an Ubuntu distribution
- Docker Desktop with WSL integration enabled for Ubuntu
- Git configured inside WSL

Run all commands below inside Ubuntu WSL, not PowerShell:

```bash
cd /mnt/e/workspace/Media_player
```

## Quick start

Create the local environment file. `.env` is ignored by Git.

```bash
cp .env.example .env
```

Build and start all six services:

```bash
docker compose up --build -d
docker compose ps
```

Wait until every service reports `healthy`, then check the API:

```bash
curl http://127.0.0.1:8000/health
curl http://127.0.0.1:8000/jobs
```

Expected responses from an empty installation:

```json
{"status":"ok"}
[]
```

Then open the app:

```text
http://localhost:3000
```

Choose a video file and click Upload. The status line moves through
`Queued...` -> `Processing...` -> `Processing complete.` and the player appears
with the processed result.

Use `localhost:3000` (or `127.0.0.1:3000`) exactly. The API's CORS allowlist
only accepts those two origins, and opening `frontend/index.html` directly from
disk will not work - the browser sends a `null` origin and every request is
blocked.

Interactive API documentation is available at <http://127.0.0.1:8000/docs>.

View logs or stop the stack without deleting data:

```bash
docker compose logs -f api
docker compose down
```

`docker compose down -v` also deletes the local PostgreSQL, Redis, and MinIO volumes. Use it only when a completely clean local state is intended.

## Database migrations

The API container applies pending migrations before Uvicorn starts. Migrations can also be inspected or applied manually:

```bash
docker compose run --rm api alembic current
docker compose run --rm api alembic upgrade head
```

Inspect the current table directly:

```bash
docker compose exec postgres \
  psql -U flickpond -d flickpond -c '\d+ jobs'
```

The `jobs` table uses UUID identifiers and exactly four lowercase states:

```text
queued -> processing -> done
                     -> failed
```

Database constraints reject unknown states, a `done` row without `output_key`, and a `failed` row without a readable error. Indexes exist on `status` and `created_at`.

## Upload API

```http
POST /upload
```

Multipart form with a single `file` field. Stores the object, inserts the job
row, enqueues the job id, and returns immediately - it never waits for
processing:

```json
{"job_id": "9b4595b8-9bd3-4a71-b99d-488c7c7f381c"}
```

Returns `202`. Rejections:

| Status | Body | When |
| --- | --- | --- |
| `413` | `{"error": "file too large"}` | Above the 100MB limit. Checked against `Content-Length` before the body is read, and again against the bytes that arrived. |
| `415` | `{"error": "unsupported media type"}` | The declared `Content-Type` is not an accepted video type. |
| `415` | `{"error": "file content is not a recognized video format"}` | The bytes are not a recognised video container. The declared type is attacker-supplied — a browser fills it in from the file extension — so the content is checked too. |

The stored object is served back under the *sniffed* type, not the client's claim.

## Status API

Get one job:

```http
GET /jobs/{uuid}
```

Get a page of jobs, newest first:

```http
GET /jobs?limit=50&offset=0
```

`limit` defaults to 50 and is capped at 200; `offset` defaults to 0. Ordering is `created_at` descending with `id` breaking ties, so paging never repeats or drops a row. Out-of-range values return `422`.

Queued response:

```json
{
  "id": "9b4595b8-9bd3-4a71-b99d-488c7c7f381c",
  "filename": "demo.mp4",
  "status": "queued"
}
```

Completed response:

```json
{
  "id": "9b4595b8-9bd3-4a71-b99d-488c7c7f381c",
  "filename": "demo.mp4",
  "status": "done",
  "output_url": "http://127.0.0.1:9000/videos/...signed-query..."
}
```

Internal `source_key` and `output_key` values never appear in HTTP responses. A completed job receives a time-limited `output_url`; the frontend must use this URL for playback.

An unknown UUID returns:

```json
{"error":"not found"}
```

## Shared repository interface

`app.repositories.jobs` exposes asynchronous functions for the upload API and worker:

```python
create_job(session, filename=..., source_key=..., job_id=None)
get_job(session, job_id)
list_jobs(session, *, limit=50, offset=0)
mark_processing(session, job_id)
mark_done(session, job_id, output_key=...)
mark_failed(session, job_id, error=...)
```

Integration rules:

- B calls `create_job` after storing the source object, then enqueues the returned job ID.
- A is the sole caller of `mark_processing`, `mark_done`, and `mark_failed`.
- C's GET endpoints only read state and create output URLs.
- E reads `output_url`, never `output_key`, and stops polling on `done` or `failed`.

The transition functions use conditional SQL updates. Repeating or skipping a transition raises `InvalidJobTransitionError` instead of silently overwriting the row.

## MinIO addresses

Two endpoint variables are intentional:

- `MINIO_ENDPOINT=minio:9000` is the internal Compose address used by API and worker code.
- `MINIO_PUBLIC_ENDPOINT=127.0.0.1:9000` is embedded in signed URLs returned to the browser. The deployment sets it to `flickpond.com`.

Do not generate browser URLs with `minio:9000`; that hostname only resolves inside the Compose network. Set `MINIO_PUBLIC_ENDPOINT` to the public storage hostname when deploying remotely.

The **host is part of a SigV4 signature**, so the two endpoints cannot be swapped after a URL is signed, and `MINIO_PUBLIC_USE_SSL` must match the page's scheme or the browser blocks the result as mixed content.

## Tests and code quality

Start the dependency services, then run the full Python 3.12 test suite:

```bash
docker compose up -d postgres redis minio
docker compose run --rm \
  -e RUN_POSTGRES_TESTS=1 \
  -v "$PWD:/workspace" -w /workspace api \
  sh -c "python -m pip install -e '.[dev]' && \
         ruff check . && ruff format --check . && \
         pytest --cov=app"
```

Without `RUN_POSTGRES_TESTS=1`, tests that require a real PostgreSQL service are skipped. The configured coverage gate is 80%.

The frontend has its own suite (Node 20+ required):

```bash
cd frontend
npm install
npm test                                    # unit tests, mocked fetch
RUN_LIVE_TESTS=1 npx vitest run app.live.test.js   # against the running stack
```

## Environment variables

| Variable | Meaning | Local default |
| --- | --- | --- |
| `POSTGRES_DSN` | Async API and worker database connection | Compose PostgreSQL service |
| `REDIS_HOST` / `REDIS_PORT` | Queue connection | `redis` / `6379` |
| `REDIS_QUEUE` | Shared RQ queue | `video_jobs` |
| `MINIO_ENDPOINT` | Internal object-store address | `minio:9000` |
| `MINIO_PUBLIC_ENDPOINT` | Browser-accessible signed-URL address | `127.0.0.1:9000` |
| `MINIO_BUCKET` | Source and output object bucket | `videos` |
| `MINIO_REGION` | Signing region | `us-east-1` |
| `MINIO_USE_SSL` | Whether MinIO uses TLS | `false` |

See [`.env.example`](.env.example) for the complete list. Never commit `.env` or real credentials.

## Git workflow

Each role has an integration branch. Create a short-lived personal branch from the role branch, open a pull request back to that role branch, and merge the completed role branch into `main` only after integration testing.

Example for track C:

```bash
git switch C-status-endpoints-db
git pull --ff-only
git switch -c <name>/c-status-db

# After committing and testing:
git push -u origin <name>/c-status-db
```

Before committing, confirm that `git config user.email` belongs to the contributor's GitHub account so the work appears in the contribution history.

## What is and is not built

Built, through sprint 2:

```text
register/login -> upload -> queued -> processing (FFmpeg 720p) -> done | failed
                                   -> status API -> playback URL
                     reaper recovers rows abandoned by a dead worker
```

Still out of scope: retries, format selection, quotas, resumable or multipart
upload, cloud orchestration, and instant session revocation (the JWT is
stateless and stays valid until it expires — see
[`docs/s2-03-auth-design.md`](docs/s2-03-auth-design.md) §1).

Upload is also **not atomic**: the object lands in MinIO before the job row is
committed, so a crash between the two leaves an orphaned object. The reaper
sweeps those; the reasoning is in
[`docs/sprint2-backlog.md`](docs/sprint2-backlog.md).
