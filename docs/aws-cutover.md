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

Copy the dump to `/srv/` on the staging core. Stop staging writers and make
sure D's staging worker desired count is zero, then restore through a temporary
PostgreSQL client container:

```bash
dc stop api reaper queue-metrics
docker run --rm -i --env-file .env postgres:16-alpine \
  sh -c 'exec pg_restore --dbname="$POSTGRES_DSN" --clean --if-exists \
  --no-owner --no-privileges' < /srv/flickpond-rehearsal.dump
dc up -d
```

Because `POSTGRES_DSN` ends in `sslmode=require`, `pg_restore` also refuses a
plaintext connection. Verify the old users and jobs exist, an old object's key
still resolves after its matching object is copied to S3, and `alembic current`
reports the expected revision.

## 8. Friday preparation

- Every track has an open, green PR and the cutover commit is known.
- Lower the `flickpond.com` A-record TTL to 300 seconds at Spaceship.
- Create the AWS monthly budget alert at **USD 80** and confirm its recipient.
- Take a fresh size reading of MinIO (expected about 465 MB).
- Confirm the Alibaba host has enough free disk for one dump and one object
  export.
- Confirm the rollback host will be kept untouched for one week.

## 9. Saturday cutover

Decide at **12:00 Asia/Singapore**. Announce the write freeze before stopping
anything.

### a. Freeze writes and take the final database dump

```bash
ssh -i <key>.pem root@47.238.64.156
cd /root/Media_player
docker compose stop api worker reaper
docker compose exec -T postgres \
  pg_dump -U flickpond -Fc flickpond > /root/flickpond-cutover.dump
```

Copy the dump to `/srv/flickpond-cutover.dump` on the AWS core. Stop AWS
writers and set the production worker group desired count to zero, then:

```bash
dc stop api reaper queue-metrics
docker run --rm -i --env-file .env postgres:16-alpine \
  sh -c 'exec pg_restore --dbname="$POSTGRES_DSN" --clean --if-exists \
  --no-owner --no-privileges' < /srv/flickpond-cutover.dump
```

### b. Copy MinIO to S3

On the current server, export the bucket without putting credentials in the
command line:

```bash
docker run --rm --network media_player_default --env-file .env \
  -v /root/minio-export:/out --entrypoint sh minio/mc \
  -c 'mc alias set old http://minio:9000 "$MINIO_ACCESS_KEY" "$MINIO_SECRET_KEY" && mc mirror old/videos /out'
```

Copy `/root/minio-export` to `/srv/minio-export` on the core, then:

```bash
aws s3 sync /srv/minio-export "s3://$(sed -n 's/^MINIO_BUCKET=//p' .env)/"
```

The object keys remain unchanged, so restored rows need no rewrite. Compare
object counts and total bytes before continuing.

### c. Certificates, services and DNS

Copy `deploy/certbot/conf/` from the current server to the same path on the
core. Configure the canonical `flickpond.com` TLS block and the `www` redirect,
then:

```bash
git fetch origin
git reset --hard <cutover-commit>
deploy/aws/fetch-secrets.sh production
dc up -d --build
curl -fsS http://127.0.0.1/api/health
```

Point the `flickpond.com` A record to the core's Elastic IP. Do not widen the
session cookie domain; `www` must redirect to the bare domain (T-24).

### d. Production verification

- Sign in and play an old video.
- Upload a new 4K phone clip and play it at 2160p.
- Complete and resume a 1 GB direct upload.
- Confirm thumbnails and editing.
- Delete a test account and confirm its S3 objects are gone.
- Confirm the health alarm is armed.
- With the queue empty, confirm the worker group returns to its scheduled
  minimum.

## 10. Rotate the operator password

The old operator password appeared in chat logs. Rotate it on the AWS core
after production verification, and on the rollback host while that host is
retained. Enter it interactively; never place it in shell history or chat.

```bash
dc exec api python -c "
import asyncio, getpass
from sqlalchemy import update
from app.database import get_session_factory
from app.models.user import User
from app.services.security import hash_password
email = input('Operator email: ').strip().lower()
password = getpass.getpass('New password (12+ characters): ')
if len(password) < 12 or password != getpass.getpass('Again: '):
    raise SystemExit('Too short, or the two did not match. Nothing changed.')
async def main():
    async with get_session_factory()() as session:
        result = await session.execute(update(User).where(User.email == email).values(password_hash=hash_password(password)))
        await session.commit()
        print('Updated' if result.rowcount == 1 else 'No such user; nothing changed.')
asyncio.run(main())
"
```

Signing in with the old password must fail. Existing JWT sessions remain valid
until their 30-minute maximum lifetime expires.

## 11. Rollback

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
