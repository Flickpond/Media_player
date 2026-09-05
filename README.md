# Flickpond

Flickpond is the SWE5006 practice-module project for an asynchronous video upload and processing platform. Sprint 1 proves the complete request path with a copy job in place of real FFmpeg transcoding:

```text
browser -> FastAPI -> MinIO + PostgreSQL + Redis queue
                                      |
                                      v
                              one of N workers
                                      |
                                      v
                         processing -> done/failed
                                      |
                                      v
                         browser polls the status API
```

The Sprint 1 source of truth is [`docs/sprint1-plan.md`](docs/sprint1-plan.md). The shared schema and API boundary are in [`docs/contract.md`](docs/contract.md). For what has actually been built, by whom, and what broke along the way, see [`docs/sprint1-report.md`](docs/sprint1-report.md) - keep it updated as the sprint runs.

## Architecture

| Component | Responsibility | Default local address |
| --- | --- | --- |
| Frontend | Upload form, 2s status polling, playback | `http://localhost:3000` |
| FastAPI | Upload and job-status HTTP API | `http://127.0.0.1:8000` |
| PostgreSQL | Durable job metadata and processing state | `127.0.0.1:5432` |
| Redis + RQ | Delivery of job IDs to workers | `127.0.0.1:6379` |
| MinIO | Original and processed video objects | API `127.0.0.1:9000`, console `127.0.0.1:9001` |
| Worker | Copy job and one-way state transitions | Internal Compose service |

The queue coordinates work, PostgreSQL records state, and MinIO stores the video bytes. Workers remain stateless, so any worker replica can process any queued job.

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
|   |-- services/storage.py       # MinIO client used by the upload path
|   |-- worker/                   # RQ worker, state machine, copy step
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
|   |-- contract.md               # Sprint 1 integration contract
|   |-- sprint1-report.md         # Living record: contributions, bugs, evidence
|   |-- c-status-db.md            # Detailed C-track commands
|   |-- proposal.md               # Full module proposal
|   `-- sprint1-plan.md           # Current Sprint 1 plan
|-- Dockerfile                    # Python 3.12 API image
|-- docker-compose.yml            # Frontend, API, worker, Redis, PostgreSQL, MinIO
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

Returns `202`, or `400 {"error": "file too large"}` above the 100MB limit.

## Status API

Get one job:

```http
GET /jobs/{uuid}
```

Get all jobs, newest first:

```http
GET /jobs
```

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
list_jobs(session)
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
- `MINIO_PUBLIC_ENDPOINT=127.0.0.1:9000` is embedded in signed URLs returned to the browser.

Do not generate browser URLs with `minio:9000`; that hostname only resolves inside the Compose network. Set `MINIO_PUBLIC_ENDPOINT` to the public storage hostname when deploying remotely.

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

## Sprint 1 boundaries

Sprint 1 uses a copy operation as the processing job. Real FFmpeg transcoding, retries, authentication, format selection, quotas, and cloud orchestration are outside this sprint. The immediate integration target is:

```text
upload -> queued -> processing -> done/failed -> status API -> playback URL
```
