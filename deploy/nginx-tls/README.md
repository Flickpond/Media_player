# TLS server block for a deployment

Empty in a fresh checkout, and mounted read-only into the frontend container at
`/etc/nginx/app-tls`. `nginx.conf` ends with `include /etc/nginx/app-tls/*.conf;`,
so a file here adds an HTTPS server without changing anything a developer runs.

**Why it is not simply in `nginx.conf`:** `ssl_certificate` pointing at a file
that does not exist stops nginx from starting. Committing a TLS block would
break `docker compose up` on every machine that has no certificate — the same
"a clone of main does not run" failure as BUG-03 in `sprint1-report.md`.

## Issuing a certificate

The challenge path is already served, so this works against a running stack:

```bash
docker run --rm \
  -v "$PWD/deploy/certbot/conf:/etc/letsencrypt" \
  -v "$PWD/deploy/certbot/www:/var/www/certbot" \
  certbot/certbot certonly --webroot -w /var/www/certbot \
  -d example.com -d www.example.com \
  --email you@example.com --agree-tos --no-eff-email --non-interactive
```

Add `--dry-run` first. A failed real attempt still counts against Let's
Encrypt's rate limit; a dry run does not.

## Turning TLS on

Create `deploy/nginx-tls/tls.conf`, substituting your hostnames:

```nginx
server {
    listen 443 ssl;
    http2 on;
    server_name example.com www.example.com;

    ssl_certificate     /etc/letsencrypt/live/example.com/fullchain.pem;
    ssl_certificate_key /etc/letsencrypt/live/example.com/privkey.pem;
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

# Redirect the real hostnames to HTTPS. A more specific server_name beats the
# default_server in nginx.conf, so requests by IP still answer over plain HTTP
# and the container healthcheck keeps working.
server {
    listen 80;
    server_name example.com www.example.com;

    # Only the challenge path, not locations.conf -- that file defines
    # `location /` and so does the redirect below, and nginx refuses to start
    # on a duplicate location.
    include /etc/nginx/app-locations/acme.conf;

    location / {
        return 301 https://$host$request_uri;
    }
}
```

Then:

```bash
docker compose up -d frontend
curl -sI https://example.com | head -1
```

## Two settings that must change with it

`MINIO_PUBLIC_ENDPOINT` becomes the public hostname with **no port**, and
`MINIO_PUBLIC_USE_SSL=true`. Presigned URLs are handed to the browser, so on an
HTTPS page an `http://` URL is blocked as mixed content and playback dies
silently. `MINIO_USE_SSL` stays `false` — the internal client talks to
`minio:9000` over plain HTTP inside the compose network.

## Renewal

Automated, but it needs turning on. Put this in the deployment's `.env`:

```
COMPOSE_PROFILES=tls
```

That starts the `certbot` service, which runs `certbot renew` every 12 hours.
`renew` is a no-op until roughly 30 days before expiry, so running it often
costs nothing and gives a transient failure many chances before the certificate
actually lapses.

**Reloading nginx is the half people forget.** nginx reads the certificate once
at startup and holds it in memory. Certbot rewriting the file on disk changes
nothing until the master process re-reads it — so without a reload the site
serves an *expired* certificate while a valid one sits on disk beside it. The
`frontend` service reloads itself every 12 hours for exactly this reason.

Check it is working:

```bash
docker compose logs certbot          # a heartbeat line every 12h

# A manual dry run. --entrypoint is required: the service overrides the image's
# entrypoint with the renewal loop, so without it `renew --dry-run` becomes
# arguments to that loop, which ignores them and runs forever.
docker compose run --rm --entrypoint certbot certbot   renew --webroot -w /var/www/certbot --dry-run

echo | openssl s_client -connect example.com:443 -servername example.com 2>/dev/null   | openssl x509 -noout -dates
```

The dry run exercises the real challenge path without touching the rate limit.

**If it says "Another instance of Certbot is already running"**, a previous
`docker compose run` is still going. Interrupting the command kills the docker
*client*, not the container — it keeps running and keeps certbot's lock. Clear
it with:

```bash
docker ps -a --filter name=certbot-run --format '{{.Names}}' | xargs -r docker rm -f
```
