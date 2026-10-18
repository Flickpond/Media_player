#!/usr/bin/env bash
# Move production's data from the Alibaba host onto the AWS core, which then
# serves flickpond.com once its A record points at the core's Elastic IP.
#
#   deploy/aws/promote.sh              # rehearsal: production stays online
#   deploy/aws/promote.sh --cutover    # freeze production's writers first
#
# Run from a machine that can ssh to both hosts. Nothing passes through this
# machine's disk: the database dump, the objects and the certificate are piped
# from one host to the other. Safe to run again -- the restore replaces the
# database, the object copy is incremental, the certificate copy overwrites.
#
# What it does not do: change DNS. That is the last step, by hand, and the
# script ends by saying exactly what to set. Rollback: docs/aws-cutover.md.
#
# Override for your machine (defaults are the team's hosts):
#   PROD_SSH  command that opens a shell on the Alibaba host
#   CORE_SSH  command that opens a shell on the AWS core
#   On Windows, Git Bash's ssh may not read the AWS key; point CORE_SSH at
#   /c/Windows/System32/OpenSSH/ssh.exe with the key's Windows path.
set -euo pipefail

PROD_SSH=${PROD_SSH:-"ssh -o BatchMode=yes -i alikey(1).pem root@47.238.64.156"}
CORE_SSH=${CORE_SSH:-"ssh -o BatchMode=yes -i $HOME/.ssh/flickpond-staging.pem ec2-user@47.128.86.77"}
DOMAIN=flickpond.com
CORE_IP=47.128.86.77

mode=rehearsal
if [[ ${1:-} == --cutover ]]; then
  mode=cutover
elif [[ -n ${1:-} ]]; then
  echo "usage: $0 [--cutover]" >&2
  exit 2
fi

prod() { $PROD_SSH "cd /root/Media_player && $1"; }
core() { $CORE_SSH "cd /srv/Media_player && $1"; }
step() { printf '\n=== %s\n' "$*"; }

# Compose on the core, with the AWS overlays.
DC='sudo docker compose -f docker-compose.yml -f deploy/aws/compose.aws.yml -f deploy/aws/compose.core.yml'

step "0. Both hosts answer"
prod 'git log --oneline -1 && docker compose ps --format "{{.Name}} {{.Status}}" | grep -c healthy'
core 'git log --oneline -1 && sudo test -f .env && echo core-env-present'

if [[ $mode == cutover ]]; then
  step "1. Freeze production's writers (the page stays up; uploads and edits stop)"
  prod 'docker compose stop api worker reaper'
else
  step "1. Rehearsal: production stays online (pg_dump is consistent while it runs)"
fi

step "2. Database: production -> RDS"
prod 'docker compose exec -T postgres pg_dump -U flickpond -Fc flickpond' \
  | $CORE_SSH "sudo tee /srv/flickpond-$mode.dump > /dev/null && sudo chmod 600 /srv/flickpond-$mode.dump"
core "sudo ls -la /srv/flickpond-$mode.dump"
# Nothing may write while the database is replaced; workers find no API to
# report to either way, and the reaper is stopped with it.
core "$DC stop api reaper queue-metrics"
# The app's DSN is SQLAlchemy's (postgresql+asyncpg://...?ssl=require);
# pg_restore needs libpq's form of the same address. TLS stays required.
# The dump is root-only, so root reads it; a plain redirect would run as
# the login user and be refused.
core "sudo cat /srv/flickpond-$mode.dump | sudo docker run --rm -i --env-file .env postgres:16-alpine sh -c '
  dsn=\$(printf %s \"\$POSTGRES_DSN\" | sed -e s/+asyncpg// -e s/ssl=require/sslmode=require/)
  exec pg_restore --dbname=\"\$dsn\" --clean --if-exists --no-owner --no-privileges
'"
# The API applies any newer migration as it starts.
core "$DC up -d api reaper queue-metrics && sleep 15 && $DC exec -T api alembic current"

step "3. Objects: MinIO -> S3 (incremental)"
# Through the production API's own storage client: no storage credential is
# typed or printed anywhere. Streams one tar of every object. A one-off
# container, not `exec`: in a cutover the API is already stopped.
prod 'docker compose run --rm --no-deps -T api python -c "
import sys, tarfile, io
from app.services.minio_client import internal_client, bucket
client, name = internal_client(), bucket()
with tarfile.open(fileobj=sys.stdout.buffer, mode=\"w|\") as tar:
    for obj in client.list_objects(name, recursive=True):
        response = client.get_object(name, obj.object_name)
        try:
            data = response.read()
        finally:
            response.close(); response.release_conn()
        info = tarfile.TarInfo(obj.object_name); info.size = len(data)
        tar.addfile(info, io.BytesIO(data))
"' | $CORE_SSH 'sudo mkdir -p /srv/minio-export && sudo tar -C /srv/minio-export -xf -'
core 'bucket=$(sudo sed -n "s/^MINIO_BUCKET=//p" .env)
  sudo aws s3 sync /srv/minio-export "s3://$bucket/" --no-progress --only-show-errors
  echo "exported: $(sudo find /srv/minio-export -type f | wc -l) objects, $(sudo du -sh /srv/minio-export | cut -f1)"
  echo "in S3:    $(aws s3 ls "s3://$bucket/" --recursive --summarize | tail -2 | tr "\n" " ")"'

step "4. Certificate for $DOMAIN, and its server blocks"
prod 'tar -C deploy/certbot -cf - conf' \
  | $CORE_SSH 'sudo tar -C /srv/Media_player/deploy/certbot -xf - && sudo test -f /srv/Media_player/deploy/certbot/conf/live/flickpond.com/fullchain.pem && echo certificate-in-place'
# Written only once the certificate is there: a block naming a missing
# certificate stops nginx from starting at all.
$CORE_SSH "sudo tee /srv/Media_player/deploy/nginx-tls/$DOMAIN.conf > /dev/null" <<NGINX
server {
    listen 443 ssl;
    http2 on;
    server_name $DOMAIN;

    ssl_certificate     /etc/letsencrypt/live/$DOMAIN/fullchain.pem;
    ssl_certificate_key /etc/letsencrypt/live/$DOMAIN/privkey.pem;
    ssl_protocols       TLSv1.2 TLSv1.3;
    ssl_prefer_server_ciphers off;
    ssl_session_cache   shared:SSL:10m;
    ssl_session_timeout 1d;

    root /usr/share/nginx/html;
    index index.html;
    client_max_body_size 101m;

    add_header X-Content-Type-Options "nosniff" always;
    add_header X-Frame-Options "SAMEORIGIN" always;
    add_header Referrer-Policy "strict-origin-when-cross-origin" always;

    include /etc/nginx/app-locations/locations.conf;
}

# One canonical origin (T-24): www redirects rather than serving a second copy.
server {
    listen 443 ssl;
    http2 on;
    server_name www.$DOMAIN;

    ssl_certificate     /etc/letsencrypt/live/$DOMAIN/fullchain.pem;
    ssl_certificate_key /etc/letsencrypt/live/$DOMAIN/privkey.pem;
    ssl_protocols       TLSv1.2 TLSv1.3;

    return 301 https://$DOMAIN\$request_uri;
}

server {
    listen 80;
    server_name $DOMAIN www.$DOMAIN;

    include /etc/nginx/app-locations/acme.conf;

    location / {
        return 301 https://\$host\$request_uri;
    }
}
NGINX
# Test before restarting: a config nginx refuses would take the site down.
if ! core "$DC exec -T frontend nginx -t"; then
  $CORE_SSH "sudo rm -f /srv/Media_player/deploy/nginx-tls/$DOMAIN.conf"
  echo "nginx refused the $DOMAIN blocks; removed them, nothing restarted" >&2
  exit 1
fi
core "$DC restart frontend && sleep 5 && $DC ps frontend --format '{{.Status}}'"

step "5. $DOMAIN answers from the AWS core (before DNS moves)"
curl -fsS --resolve "$DOMAIN:443:$CORE_IP" "https://$DOMAIN/api/health"
echo
curl -sS -o /dev/null -w 'www redirect: %{http_code} -> %{redirect_url}\n' \
  --resolve "www.$DOMAIN:443:$CORE_IP" "https://www.$DOMAIN/"

if [[ $mode == cutover ]]; then
  step "Done. Now, at Spaceship: set the A record for $DOMAIN (and www) to $CORE_IP."
  echo "Production's writers stay stopped; its page still serves the old data until DNS moves."
  echo "Rollback: docs/aws-cutover.md, section 11."
else
  step "Rehearsal done. Production was not changed; DNS still points at Alibaba."
fi
