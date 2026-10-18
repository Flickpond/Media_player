# Flickpond — working notes

Asynchronous video upload and processing platform. Five-person team project;
sprint 5 is complete, and production runs on AWS since sprint 6.

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
| [`docs/known-traps.md`](docs/known-traps.md) | 25 traps already hit here. **Most fail silently.** |
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
2. **Only nginx is public.** In the local and current-server Compose stack,
   PostgreSQL, Redis, MinIO and the API are pinned to `127.0.0.1` and that is
   not configurable; use an SSH tunnel. On AWS, RDS and ElastiCache have no
   public address and admit only the core and worker security groups. Do not
   add a CIDR ingress rule, even temporarily. The API enforces per-user
   ownership; the datastores do not, so direct access still reaches every
   user's data, and write access to RQ is code execution on a worker.
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

**<https://flickpond.com>** — AWS `ap-southeast-1` since **19 Oct 2026**
(sprint 6 cutover, `docs/aws-cutover.md`). The stack proven as staging became
production, so its AWS resources keep `staging` in their names.

| Part | What |
|---|---|
| Core `47.128.86.77` (Elastic IP) | `t4g.small`, `/srv/Media_player`: nginx, api, reaper, queue-metrics. No workers, no local datastores |
| Datastores | RDS PostgreSQL and ElastiCache Valkey, private, TLS-only. Secrets in SSM `/flickpond/staging/*` |
| Video | S3 `flickpond-staging-videos-919e62dc`; HLS segments through CloudFront |
| Workers | Spot group `flickpond-staging-workers`, 0–4, booting `flickpond/worker:<sha>` from ECR. One by day (08–24 Asia/Shanghai), none overnight; the API wakes it when a job is queued |
| Infra | `infra/terraform` (base), `infra/registry`, `infra/workers`; state in S3 |

TLS via Let's Encrypt on the core, renewed by the `certbot` service; 80
redirects to 443; `www` redirects to the bare domain (T-24).

Deploy the core (SSH from PowerShell — Git Bash's ssh cannot read this key):

```bash
ssh -i ~/.ssh/flickpond-staging.pem ec2-user@47.128.86.77
cd /srv/Media_player
sudo git fetch origin && sudo git reset --hard origin/main     # fetch first (T-20)
sudo bash deploy/aws/fetch-secrets.sh staging                  # .env from SSM
sudo docker compose -f docker-compose.yml -f deploy/aws/compose.aws.yml \
  -f deploy/aws/compose.core.yml up -d --build                 # --build (T-21)
```

**Workers do not deploy with the core.** They boot the image the launch
template pins: after a worker change, `infra/workers`'s `worker_image` moves to
the new commit's tag (CI pushes one per merge to `main`) and is applied.

Verify by asking the application, not the environment:

```bash
sudo docker compose -f docker-compose.yml -f deploy/aws/compose.aws.yml \
  -f deploy/aws/compose.core.yml exec api python -c \
  "from app.config import get_settings as g; s=g(); print(s.worker_group_name, s.redis_ssl)"
curl -fsS https://flickpond.com/api/health
```

**Rollback until 26 Oct 2026:** the Alibaba host `47.238.64.156` is kept
untouched with its writers stopped — `docs/aws-cutover.md` §9. Then one final
dump, and it is shut down.

## Conventions

- Python 3.12, FastAPI, SQLAlchemy 2 async, Alembic, RQ, MinIO.
- Line length 100. Ruff lint rules: `E, F, I, UP, B, ASYNC`.
- Coverage gate `fail_under = 80` in `pyproject.toml`; actual is 85.88%
  (`pytest --cov=app`, 11 Sep 2026).
- Comments explain *why*, not *what*. The existing code is written that way —
  match it.
- Tests are named as sentences describing the behaviour being protected.
