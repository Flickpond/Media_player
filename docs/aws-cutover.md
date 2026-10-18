# Moving Flickpond to the split AWS design

The sprint 5 staging rehearsal and production cutover, in order. Commands are
written to be copied; replace values in angle brackets locally and never paste
credentials into a terminal shared with somebody else.

| | Current production | AWS production |
|---|---|---|
| Public entry point | Alibaba ECS `47.238.64.156` | `t4g.small` core: nginx, API, reaper, queue metrics |
| Database | Local PostgreSQL container | Private RDS PostgreSQL 16, TLS required |
| Queue | Local Redis container | Private ElastiCache Valkey, TLS and auth token |
| Storage | Local MinIO | Private S3 bucket through instance roles |
| Encoding | Two local workers | Spot worker group, queue-depth scaling |
| Built by | Hand | `infra/terraform` (A) + `infra/workers` (D) |

**The Saturday rule:** move only if staging passed the datastore, worker,
interruption, restore and 4K gates below. Otherwise leave production on the
current server. It already runs sprint 4 and is the safer result.

---

## 1. Put Terraform state in the remote backend

State contains generated datastore passwords. Before D applies the worker
module, create the private versioned state bucket and migrate the existing
local state. Follow [`infra/terraform/README.md`](../infra/terraform/README.md)
exactly; use a separate backend key for staging and production.

Checks:

```bash
aws s3api get-public-access-block --bucket <state-bucket>
aws s3api get-bucket-versioning --bucket <state-bucket>
```

All four public-access flags must be `true`; versioning must be `Enabled`.
Run `terraform plan` a second time after migration. It must describe the same
infrastructure, not a fresh copy of it.

## 2. Review, then create the managed datastores

Read [`datastore-review.md`](datastore-review.md). Someone other than the
person running `apply` must read the plan before the first apply that creates
RDS or ElastiCache.

For staging, include both the staging HTTPS origin and `https://flickpond.com`
in `site_origins`; direct browser uploads need the exact origin in S3 CORS.
The existing Elastic IP can use `<dashed-ip>.sslip.io` as its staging name.

```bash
cd infra/terraform
terraform init -backend-config=staging.backend.hcl
terraform plan -var-file=staging.tfvars -out=staging.plan
terraform show staging.plan
# Stop here until the independent review is recorded.
terraform apply staging.plan
```

The apply also shrinks the always-on core to `t4g.small`. Its Elastic IP stays
with it. Check the contract D consumes:

```bash
terraform output core_private_ip
terraform output db_security_group_id
terraform output cache_security_group_id
terraform output db_secret_parameter
terraform output cache_secret_parameter
```

The last two outputs are parameter names, never secret values.

## 3. Configure the core host

Clone the repository on the core, create the non-secret half of `.env`, then
fetch the datastore values through its instance role:

```bash
sudo mkdir -p /srv && sudo chown ec2-user /srv && cd /srv
git clone https://github.com/Flickpond/Media_player.git && cd Media_player
cp deploy/aws/env.aws.example .env
# Fill MINIO_BUCKET from `terraform output bucket_name`, generate JWT_SECRET
# with `openssl rand -base64 48`, and set METRICS_ENVIRONMENT=staging.
deploy/aws/fetch-secrets.sh staging
chmod 600 .env

dc() {
  docker compose -f docker-compose.yml \
    -f deploy/aws/compose.aws.yml \
    -f deploy/aws/compose.core.yml "$@"
}
dc config --quiet
dc up -d --build
```

The core overlay deliberately runs zero workers and replaces the local
PostgreSQL and Redis services with empty placeholders. Those names must still
resolve because the base Compose file uses health-gated dependencies; the
application itself connects to RDS and ElastiCache from `.env`.

Issue the staging certificate as described in
[`deploy/nginx-tls/README.md`](../deploy/nginx-tls/README.md), then restart the
frontend so nginx reads it.

Checks — ask the application, not only its environment:

```bash
dc ps
dc exec api python -c "from app.config import get_settings as g; s=g(); \
print(s.postgres_dsn.split('@')[-1], s.redis_host, s.redis_ssl)"
dc exec api python -c "from app.services.minio_client import internal_client, bucket; \
print(internal_client().bucket_exists(bucket()))"
dc run --rm queue-metrics --once --dry-run
```

Expect every running service healthy, an RDS hostname, an ElastiCache hostname
and `True`, then `True` for the S3 bucket. `dc ps` must show no worker and no
local PostgreSQL or Redis image holding user data.

## 4. Prove the network boundary

From the core and from one worker, connect to RDS on 5432 with TLS and Valkey
on 6379 with TLS. From a machine outside AWS, both endpoints must be
unreachable. Test the actual endpoint, not only the Terraform plan.

```bash
# Outside AWS: both should time out, never connect.
nc -vz -w 5 <rds-endpoint> 5432
nc -vz -w 5 <valkey-endpoint> 6379
```

Also inspect both security groups. Their inbound rules may reference only the
core and worker security groups; no CIDR rule is allowed.

## 5. Prove the worker path and interruption recovery

D applies the worker group only after the datastore review. With staging idle,
queue a video and verify a spot worker starts, takes it and writes the result
to S3. Then run the agreed interruption test:

1. start a long encode;
2. send the worker `docker stop --time 100` (or use AWS FIS);
3. verify the job returns to `queued` and its partial output is gone;
4. verify another worker completes it;
5. repeat only as needed to show the third-interruption cap fails readably.

Separately, a `docker kill` test proves the heartbeat/reaper path for a worker
that disappears without warning.

## 6. Time a real 4K phone clip

Upload a real 4K phone clip of about 60 seconds. A generated test pattern is
not evidence: it compresses too easily. Record upload → MP4, MP4 → ladder, and
total time from the job and worker logs.

Passes when the ladder reaches `ready`, the player offers 2160p, any kept
60-fps label is correct, and the encode is comfortably inside the timeout.
Also edit it to 720p and verify that edit's ladder tops out at 720p.

## 7. Rehearse the database move

`pg_dump` is transactionally consistent, so production can stay online for
the rehearsal. On the current server:

```bash
cd /root/Media_player
docker compose exec -T postgres \
  pg_dump -U flickpond -Fc flickpond > /root/flickpond-rehearsal.dump
```

`deploy/aws/promote.sh` (§8) does this whole step, piping the dump straight
to the core. Note for running it by hand: the app's `POSTGRES_DSN` is
SQLAlchemy's (`postgresql+asyncpg://…?ssl=require`, since #77), so
`pg_restore` needs it rewritten to libpq's form (`postgresql://…?sslmode=require`)
— the script does exactly that, and TLS stays required.

Verify the old users and jobs exist, an old object still plays after its
object is copied to S3, and `alembic current` reports the expected revision.

## 8. Promote staging to production — Tuesday 20 October

**Decided:** there is no second AWS environment. The staging stack proven in
sprint 5 — the core at `47.128.86.77`, RDS, ElastiCache, CloudFront, the
registry and the spot workers — becomes production when `flickpond.com`
points at it. Its resources keep their `staging` names; renaming would mean
recreating the datastores for nothing.

Everything is done by [`deploy/aws/promote.sh`](../deploy/aws/promote.sh),
run from a machine that can ssh to both hosts. Nothing passes through that
machine's disk: the dump, the objects and the certificate are piped host to
host.

| Step | What it does |
|---|---|
| 0 | Both hosts answer |
| 1 | `--cutover` only: stop production's `api`, `worker` and `reaper` (the page stays up; uploads and edits stop) |
| 2 | `pg_dump` on Alibaba → the core → `pg_restore --clean` into RDS over TLS; the API applies newer migrations as it starts |
| 3 | Every MinIO object, through production's own API image (no storage credential typed), → `/srv/minio-export` → `aws s3 sync` (incremental); prints both counts |
| 4 | `deploy/certbot/conf` (the `flickpond.com` certificate and its renewal config) → the core; writes the `flickpond.com` server blocks, tests them with `nginx -t`, restarts nginx |
| 5 | `https://flickpond.com/api/health` answered **by the AWS core**, through `curl --resolve`, before DNS moves |

On Windows, Git Bash's ssh cannot read the AWS key; use Windows' own client
for that side:

```bash
export CORE_SSH='/c/Windows/System32/OpenSSH/ssh.exe -o BatchMode=yes -i C:/Users/<you>/.ssh/flickpond-staging.pem ec2-user@47.128.86.77'
deploy/aws/promote.sh              # rehearsal, any time: production unchanged
```

### Tuesday, in order

1. **Rehearse** in the afternoon: `deploy/aws/promote.sh`. It must reach
   step 5 and print `{"status":"ok"}`. Sign in on
   <https://47-128-86-77.sslip.io> with a production account and play an
   old video.
2. **Announce the freeze** in the group, then
   `deploy/aws/promote.sh --cutover`. About five minutes; the objects copy
   only what changed since the rehearsal.
3. **DNS:** at Spaceship, the A records for `flickpond.com` and
   `www.flickpond.com` → `47.128.86.77`. The TTL is already 10 s.
4. **Verify** on <https://flickpond.com>:
   - sign in (everyone signs in again: the AWS core has its own JWT secret);
   - play an old video;
   - upload a new one and watch a spot worker take it;
   - edit it;
   - an operator deletes a test account.
5. **After verification:**
   - `METRICS_ENVIRONMENT` stays `staging`: the alarms and the worker group
     are named for it;
   - rewrite CLAUDE.md's deployment section;
   - keep Alibaba untouched for a week (§11).

## 9. Rollback

Point DNS back to `47.238.64.156` and start `api`, two workers and the reaper
there:

```bash
docker compose start api reaper
docker compose up -d --scale worker=2
docker compose restart frontend
```

Anything uploaded after AWS opened for writes is not on the old host. Record
those job IDs before rolling back. Keep the Alibaba server untouched for one
week, then take one final dump before shutting it down.
