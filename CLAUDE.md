# Flickpond — working notes

Asynchronous video upload and processing platform. SWE5001 team project, 5
people, sprint 2 in progress.

```
browser -> nginx -> FastAPI -> MinIO + PostgreSQL + Redis/RQ -> worker (1..N)
                                       queued -> processing -> done | failed
```

Sprint 1 shipped the pipeline with a copy job standing in for transcoding.
**Sprint 2 replaced it with FFmpeg**, added a reaper that recovers jobs left
behind by a crashed worker, and put CI in front of `main` as a required check.

The big thing still missing is **authorization** — the API has none, so the
deployment sits behind a shared-password nginx gate. That is S2-03.

## Read before coding

| File | Why |
|---|---|
| [`docs/sprint2-plan.md`](docs/sprint2-plan.md) | What to build, in what order, with acceptance criteria |
| [`docs/known-traps.md`](docs/known-traps.md) | 19 traps already hit here. **Most fail silently.** |
| [`docs/contract.md`](docs/contract.md) | Shared API and schema boundary — changing it means telling the team |
| [`docs/s2-03-auth-design.md`](docs/s2-03-auth-design.md) | The next work item's decisions: JWT in an HttpOnly cookie, schema, teardown |

Also: [`sprint1-report.md`](docs/sprint1-report.md) (what was built, bug log),
[`sprint2-backlog.md`](docs/sprint2-backlog.md) (open findings P1–P9),
[`scaling-notes.md`](docs/scaling-notes.md) (capacity analysis).

## Commands

```bash
# Run it
cp .env.example .env && docker compose up --build -d && docker compose ps

# Test — unit only, no services needed
python -m pytest -q
python -m ruff check .                              # `ruff check`, NOT `ruff format`

# Test — everything, needs the stack up
RUN_POSTGRES_TESTS=1 python -m pytest tests/ -q

# Frontend
cd frontend && npm ci && npm test
```

The app is at <http://localhost:3000>. Use `localhost:3000` or `127.0.0.1:3000`
exactly — those are the only two CORS origins.

## Rules

1. **Never commit credentials or keys.** `*.pem`, `*.key`, `deploy/auth/htpasswd`
   are gitignored; check `git diff --cached --name-only` before committing.
2. **Only nginx is published beyond loopback.** PostgreSQL, Redis, MinIO and the
   API are pinned to `127.0.0.1` in `docker-compose.yml` and that is not
   configurable. Use an SSH tunnel to reach them. The API has no authorization
   yet, so anything that reaches it can read every upload.
3. **Every file under `tests/integration/` needs the `RUN_POSTGRES_TESTS`
   skip guard.** An ungated one breaks CI and every teammate's test run.
4. **The worker is the sole writer** to `status`, `output_key`, `error`,
   `updated_at` after the API's insert. All writes go through
   `app/repositories/jobs.py`, never raw SQL. Transitions are one-way.
5. **Ask before changing another track's files** (see ownership below).
6. **`main` requires a pull request.** Direct pushes are possible with admin
   rights but bypass the team's review gate.

## Ownership

Sprint 1 split five ways and ownership follows the code into `main`:

| Track | Owns |
|---|---|
| A (this user) | `app/worker/*`, `app/queue.py`, `app/services/media_type.py` |
| B | `app/api/uploads.py`, `app/services/storage.py` |
| C | `app/api/jobs.py`, `app/repositories/`, `app/models/`, `migrations/` |
| D | `docker-compose.yml`, `nginx.conf`, `.env.example` |
| E | `frontend/*` |

## Deployment

Alibaba ECS, `47.238.64.156`, tracks `origin/main` at `/root/Media_player`.
nginx on :80 behind HTTP basic auth (a stopgap until authorization lands).
Eight containers: nginx, api, 2 workers, reaper, postgres, redis, minio.

```bash
ssh -i <key>.pem root@47.238.64.156
./deploy/auth/set-password.sh                    # rotate the gate password
./deploy/auth/verify.sh http://127.0.0.1 flickpond '<password>'
```

## Conventions

- Python 3.12, FastAPI, SQLAlchemy 2 async, Alembic, RQ, MinIO.
- Line length 100. Ruff lint rules: `E, F, I, UP, B, ASYNC`.
- Coverage gate `fail_under = 80` in `pyproject.toml`; actual is 99%.
- Comments explain *why*, not *what*. The existing code is written that way —
  match it.
- Tests are named as sentences describing the behaviour being protected.
