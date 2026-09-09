# Access gate for a published deployment

This directory is mounted read-only into the frontend container at
`/etc/nginx/app-auth`, and `nginx.conf` includes `*.conf` from it. It is empty
in the repository on purpose: local development needs no gate, and a checked-in
credential is a credential that has leaked.

## Why this exists

The API has no authentication of its own. `GET /jobs` returns every job in the
table together with a signed, working download URL for each, so anything that
can reach the API can read every upload on the system. That is tracked as **P1**
in [`../../docs/sprint2-backlog.md`](../../docs/sprint2-backlog.md), and it is
sprint 2 work because it needs an owner column and a caller identity.

Until then, a deployment that is reachable by anyone other than its developers
needs a gate in front. This is that gate. It is a stopgap and should be deleted
the moment P1 lands -- basic auth is a shared password, so it tells you nobody
uninvited got in, not who did what.

## Setting it up on a host

```bash
# 1. Create the password file (htpasswd comes from apache2-utils).
docker run --rm httpd:2.4-alpine htpasswd -nbB flickpond '<a-real-password>' \
  > deploy/auth/htpasswd

# 2. Turn the gate on.
cat > deploy/auth/auth.conf <<'CONF'
auth_basic "Flickpond";
auth_basic_user_file /etc/nginx/app-auth/htpasswd;
CONF

# 3. Reload.
docker compose up -d frontend
```

Both files are ignored by git (see the repository `.gitignore`). `/healthz`
stays exempt so the container healthcheck keeps working.

## The htpasswd file must be readable by the nginx *worker*

Leave it world-readable (`chmod 644`). It holds a password hash, not a
plaintext password, and the host it sits on should already be root-only.

`chmod 600` looks more careful and silently breaks the gate. nginx's master
process runs as root but drops its workers to the `nginx` user, and it is a
worker that opens `auth_basic_user_file`. The failure is easy to miss because
it only appears once someone supplies credentials:

| Request | With `chmod 600` |
|---|---|
| no `Authorization` header | `401` — correct, the file is never opened |
| correct username/password | **`500`** — `open() ... (13: Permission denied)` |

So an unauthenticated probe suggests the gate is working perfectly, right up
until a real user tries to log in. If you see a 500 from an authenticated
request, check `docker logs` for `Permission denied` before anything else.
