# Moving Flickpond to AWS

The steps for sprint 4's cutover, in order, with what to check after each.
Written for whoever runs it — every command is meant to be copied as is.

| | Current server | AWS |
|---|---|---|
| Host | Alibaba ECS `47.238.64.156`, Hong Kong | EC2 `c7g.2xlarge`, Singapore, Elastic IP `47.128.86.77` |
| Storage | MinIO container | S3 bucket from `terraform output bucket_name` |
| Storage credentials | Keys in `.env` | Instance role — no keys anywhere |
| Ladder | Up to 1080p | Up to 2160p |
| Built by | Hand | [`infra/terraform`](../infra/terraform/README.md) |

**The rule that decides Saturday:** move only if staging passed step 4. If it
did not, stay on the current server — it already works, and sprint 4's code
runs there with the ladder capped at 1080p.

---

## 1. Build staging

From `infra/terraform`, with the AWS CLI signed in:

```bash
docker run --rm -it -v "$PWD:/work" -w /work -v "$HOME/.aws:/root/.aws:ro" \
  hashicorp/terraform:1.9 apply -var-file=staging.tfvars
```

> **First time only — 4 Oct.** The first apply failed on the two outbound
> security-group rules (an apostrophe AWS rejects; fixed in #55). The
> instance booted without internet access, so its first-boot install never
> ran. Add `-replace=aws_instance.app` once to rebuild it; the Elastic IP
> keeps its address.

**Check:** `terraform output` prints `public_ip = "47.128.86.77"`, and

```bash
ssh -i ~/.ssh/flickpond-staging.pem ec2-user@47.128.86.77 'docker --version && docker compose version && git --version'
```

prints three versions. If SSH times out, your IP changed since
`staging.tfvars` was written: update `admin_cidrs` and apply again, or skip
SSH entirely with `aws ssm start-session --target <instance_id>`.

## 2. Give staging a name and a certificate

Staging has no DNS record of its own. `47-128-86-77.sslip.io` resolves to the
Elastic IP with no setup, and Let's Encrypt issues certificates for it. It
needs a real HTTPS origin, not an IP address: the session cookie and the
bucket's CORS both need one.

1. Add it to the bucket's CORS: in `staging.tfvars` set
   `site_origins = ["https://47-128-86-77.sslip.io", "https://flickpond.com"]`
   and apply. Both origins, so the same bucket works after the cutover.
2. On the instance:

```bash
sudo mkdir -p /srv && sudo chown ec2-user /srv && cd /srv
git clone https://github.com/Flickpond/Media_player.git && cd Media_player
cp deploy/aws/env.aws.example .env      # then fill in the FILL values
alias dc='docker compose -f docker-compose.yml -f deploy/aws/compose.aws.yml'
dc up -d --build --scale worker=2       # both flags matter (T-21, T-23)
```

3. Issue the certificate and turn TLS on exactly as in
   [`deploy/nginx-tls/README.md`](../deploy/nginx-tls/README.md), with
   `-d 47-128-86-77.sslip.io` and that name as `server_name`. Then
   `dc restart frontend`.

**Check:** `https://47-128-86-77.sslip.io` loads with a valid certificate.

## 3. Ask the application, not the environment

```bash
dc exec api python -c "from app.config import get_settings as g; s=g(); print(s.minio_endpoint, s.storage_use_instance_role)"
dc exec api python -c "from app.services.minio_client import internal_client, bucket; print(internal_client().bucket_exists(bucket()))"
dc exec worker python -c "from app.config import get_settings as g; s=g(); print(s.worker_hls_max_height, s.worker_ffmpeg_max_height)"
```

Expect `s3.ap-southeast-1.amazonaws.com True`, then `True`, then `2160 1080`.
A `False` or an access-denied error on the second means the role is not
reaching the containers: check that the instance has the profile from
`terraform output instance_profile_name` and that IMDS has a hop limit of 2.

## 4. Prove 4K works — the gate for Saturday

Register an account, then upload **a real 4K clip of about 60 seconds** —
phone footage, not a test pattern. A test pattern compresses to almost
nothing and makes the encoder look much faster than it is.

```bash
dc logs worker --no-log-prefix | grep <job id>
```

Record three times from the log lines: upload → `processing -> done` (the
MP4), `done` → `ladder ready`, and the total.

**Passes when:** the ladder reaches `ready`, the player offers 2160p, and the
ladder took well under `WORKER_FFMPEG_TIMEOUT_SECONDS` (1770 s) — under half,
so a longer upload still fits. Put the numbers in the sprint report either
way; a failure here is a result, not a problem to hide.

**Also check:** an edit of that clip (downscale to 720p) finishes and gets a
720p-topped ladder, and the Library shows the upload's thumbnail.

## 5. Saturday: the cutover

Decide at **12:00**. If moving:

**a. A day earlier — lower the DNS TTL** for `flickpond.com` to 300 seconds
at the DNS provider, so the switch takes minutes rather than hours.

**b. Stop writes on the current server**, so nothing changes during the copy:

```bash
ssh -i <key>.pem root@47.238.64.156
cd /root/Media_player && docker compose stop api worker reaper
```

The site shows errors from here until DNS moves. Say so in the group first.

**c. Copy the database.**

```bash
# on the current server
docker compose exec -T postgres pg_dump -U flickpond -Fc flickpond > /root/flickpond-cutover.dump
# copy it to AWS (from your machine)
scp -i <key>.pem root@47.238.64.156:/root/flickpond-cutover.dump .
scp -i ~/.ssh/flickpond-staging.pem flickpond-cutover.dump ec2-user@47.128.86.77:/srv/
# on AWS: the staging rows go -- they were test data
dc stop api worker reaper
dc exec -T postgres pg_restore -U flickpond -d flickpond --clean --if-exists < /srv/flickpond-cutover.dump
```

**d. Copy the objects.** Check the size first (`du -sh` on MinIO's volume);
the AWS host has 50 GB of disk. On the current server, export MinIO's bucket
to disk, then copy the folder to AWS and upload it with the role:

```bash
# on the current server
docker run --rm --network media_player_default -v /root/minio-export:/out minio/mc \
  sh -c 'mc alias set old http://minio:9000 "$MINIO_ACCESS_KEY" "$MINIO_SECRET_KEY" && mc mirror old/videos /out'
# copy /root/minio-export to /srv/minio-export on AWS (scp -r, or rsync -e ssh), then on AWS:
aws s3 sync /srv/minio-export "s3://$(grep ^MINIO_BUCKET .env | cut -d= -f2)/"
```

The object keys (`uploads/…`, `outputs/…`) are the same in both stores, so
the restored rows point at the right objects without any rewriting.
Set the network name and the `$MINIO_*` values from that server's own
`docker network ls` and `.env`.

**e. Bring the certificate across** so HTTPS works the moment DNS moves:
copy `deploy/certbot/conf/` from the current server into the same path on
AWS, add `flickpond.com` to `deploy/nginx-tls/tls.conf` as the current
server has it, and run `dc up -d`.

**f. Point DNS** — `flickpond.com` A record → `47.128.86.77`.

**g. Verify on production:** sign in; an old video still plays; a new 4K
upload plays in 4K; thumbnails show; a 1 GB upload completes with a progress
bar.

### Rolling back

Point DNS back to `47.238.64.156` and `docker compose start api worker
reaper` on the current server. **Anything uploaded on AWS after the cutover
is not on the old server** — the longer AWS runs before a rollback, the more
that is. Keep the current server, untouched, for a week before shutting it
down.

## 6. Rotate the admin password

The operator password has been in chat logs since 11 September. Rotate it on
whichever server is live after Saturday — and on the other one too, if both
are kept. You type the new password; it is never shown, logged or stored in
shell history.

```bash
docker compose exec api python -c "
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

**Check:** signing in with the old password fails and the new one works.
Sessions already open stay valid until their token expires (30 minutes at
most) — a JWT cannot be revoked early.

Keep the new password in a password manager. **Do not paste it into chat**,
including into an AI assistant: that is how the last one leaked.

## 7. Afterwards

- **Stop paying for idle staging.** If staging and production are separate
  instances, stop staging when it isn't being tested:
  `aws ec2 stop-instances --instance-ids <id>`.
- **Remote Terraform state** (sprint 5), before D's autoscaling module is
  applied: state is on one laptop today.
- **Close the old server** after a quiet week; take a final `pg_dump` first.
