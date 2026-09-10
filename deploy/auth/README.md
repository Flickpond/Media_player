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

Use the script. Do not write `htpasswd` by hand -- see the trap below.

```bash
./deploy/auth/set-password.sh                    # random password, user "flickpond"
./deploy/auth/set-password.sh alice              # random password, user "alice"
./deploy/auth/set-password.sh alice 'a-secret'   # explicit password
```

It writes both files with the right mode, reloads nginx if the stack is up, and
then verifies the gate actually gates. Rotating a password is the same command.

To check a gate you did not just create:

```bash
./deploy/auth/verify.sh http://127.0.0.1 flickpond 'the-password'
```

`verify.sh` checks three things: anonymous requests get 401, `/healthz` stays
200 so the container healthcheck keeps working, and correct credentials get 200
rather than the 500 described below.


## The 600 trap

### The htpasswd file must be readable by the nginx *worker*

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
