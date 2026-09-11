# Flickpond — working notes

Asynchronous video upload and processing platform. Five-person team project;
sprint 2 is complete and deployed.

```
browser -> nginx -> FastAPI -> MinIO + PostgreSQL + Redis/RQ -> worker (1..N)
                                       queued -> processing -> done | failed
                    reaper -> recovers rows a dead worker abandoned
```

Sprint 1 shipped the pipeline with a copy job standing in for transcoding.
**Sprint 2 replaced it with FFmpeg**, added a reaper that recovers jobs left
behind by a crashed worker, and put CI in front of `main` as a required check.

**Authorization landed too** (S2-03): accounts, per-user job ownership, and an
operator role. The shared-password nginx gate that stood in for it is gone.

## Read before coding

| File | Why |
|---|---|
| [`docs/sprint2-plan.md`](docs/sprint2-plan.md) | What to build, in what order, with acceptance criteria |
| [`docs/known-traps.md`](docs/known-traps.md) | 23 traps already hit here. **Most fail silently.** |
| [`docs/contract.md`](docs/contract.md) | Shared API and schema boundary — changing it means telling the team |
| [`docs/s2-03-auth-design.md`](docs/s2-03-auth-design.md) | Why auth is shaped the way it is: JWT in an HttpOnly cookie, schema, roles |

Also: [`sprint2-report.md`](docs/sprint2-report.md) (sprint 2 contributions, NFR
evidence, BUG-04 to BUG-12), [`sprint1-report.md`](docs/sprint1-report.md)
(sprint 1 equivalent),
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

1. **Never commit credentials or keys.** `*.pem`, `*.key` and `deploy/certbot/`
   are gitignored; check `git diff --cached --name-only` before committing.
2. **Only nginx is published beyond loopback.** PostgreSQL, Redis, MinIO and the
   API are pinned to `127.0.0.1` in `docker-compose.yml` and that is not
   configurable. Use an SSH tunnel to reach them. The API enforces per-user
   ownership since S2-03; the datastores behind it do not, so anything that
   reaches one of them directly still reads every user's data.
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

**<https://flickpond.com>** — Alibaba ECS `47.238.64.156` (cn-hongkong, so no
ICP filing needed), tracking `origin/main` at `/root/Media_player`. Eight
containers: nginx, api, 2 workers, reaper, postgres, redis, minio.

TLS via Let's Encrypt, renewed automatically; 80 redirects to 443. Sign-in is
the app's own.

```bash
ssh -i <key>.pem root@47.238.64.156
cd /root/Media_player
git fetch origin && git reset --hard origin/main      # fetch first (T-20)
docker compose up -d --build --scale worker=2         # both flags matter
```

**`--build`** or a code change silently will not ship (T-21). **`--scale
worker=2`** or the second worker silently disappears (T-23) — the replica count
is not in `docker-compose.yml`.

Verify by asking the application, not the environment:

```bash
docker compose exec api python -c "from app.config import get_settings; print(get_settings().minio_public_endpoint)"
```

## Conventions

- Python 3.12, FastAPI, SQLAlchemy 2 async, Alembic, RQ, MinIO.
- Line length 100. Ruff lint rules: `E, F, I, UP, B, ASYNC`.
- Coverage gate `fail_under = 80` in `pyproject.toml`; actual is 85.88%
  (`pytest --cov=app`, 11 Sep 2026).
- Comments explain *why*, not *what*. The existing code is written that way —
  match it.
- Tests are named as sentences describing the behaviour being protected.
