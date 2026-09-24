# S2-03 — Authorization: design decisions

**Written:** 10 September 2026 · against `main` @ `e6fcee9`
**Status:** implemented. Every decision below is now in the code; this stays
as the record of *why*, not as a plan.

This exists so nobody re-derives choices that have already been made. Every
decision here is settled and built; the reasoning is kept because the reasoning
is the part that is expensive to reconstruct.

Read alongside [`sprint2-plan.md`](sprint2-plan.md) §S2-03 for the work
breakdown, and [`known-traps.md`](known-traps.md) before touching the schema.

---

## 1. The decision

**Signed JWTs, delivered in an `HttpOnly` cookie.** Not an `Authorization`
header, not opaque server-side sessions.

The proposal requires JWT (§5.4: *"All API endpoints must require JWT
authentication"*; Phase 1: *"registration, login, JWT session, RBAC"*). Cookie
delivery keeps that requirement while removing the property that actually
hurts — a token in `localStorage` is readable by any script on the page, and
this codebase has two documented XSS vectors (T-01, T-02).

**Write this down in the report and in [`contract.md`](contract.md)**, in these
words or similar:

> Authentication uses signed JWTs per §5.4, delivered in an `HttpOnly` cookie
> rather than an `Authorization` header, so the token is not reachable by script.

Without that sentence, a reader checking against the proposal sees cookies and
records the JWT requirement as unmet — losing marks for having done the safer
thing.

### What this choice costs

Three consequences to design for, not discover:

- **Revocation is impossible.** A token stays valid until it expires. Logout
  clears the client's copy only. Mitigate with a short lifetime; add a Redis
  denylist only if instant logout becomes a requirement, and understand that
  doing so gives up the statelessness that justified JWT.
- **CSRF applies.** Cookies ride along automatically. `SameSite` handles it —
  see §4.
- **The frontend cannot read the token.** That is the point, and it means the
  UI cannot decode claims. `GET /api/me` (§5) exists for this, and logout must
  be a server endpoint because JavaScript cannot delete an `HttpOnly` cookie.

### Prerequisite that outranks all of this

**TLS**, which landed first (S2-05). Over plain HTTP the login POST and the
cookie both cross the network readable and `Secure` is a no-op -- shipping
authentication over HTTP is worse than a basic-auth gate, because it looks like
a control and is not. The site is on <https://flickpond.com>, so the `Secure`
flag now means something.

---

## 2. Schema

### `users`

| Column | Type | Notes |
|---|---|---|
| `id` | UUID, PK | matches the `jobs.id` convention |
| `email` | Text, unique, not null | the login identifier |
| `password_hash` | Text, not null | see the hashing note below |
| `role` | Text, not null, default `'user'` | `CHECK (role IN ('user','operator'))` |
| `created_at` | timestamptz, not null, `now()` | |

Constrain `role` with a `CHECK` rather than an application enum, matching how
`jobs.status` is already constrained. The database is the last line of defence
in this codebase and should stay that way.

### `jobs.owner_id`

```sql
ALTER TABLE jobs ADD COLUMN owner_id UUID REFERENCES users(id);
```

**Backfill before making it `NOT NULL`.** Existing rows have no owner, and
allowing NULL permanently means every query needs a null branch forever.

1. create a sentinel user (`legacy@flickpond.invalid`, role `user`, an
   unusable password hash)
2. `UPDATE jobs SET owner_id = <sentinel> WHERE owner_id IS NULL`
3. `ALTER TABLE jobs ALTER COLUMN owner_id SET NOT NULL`
4. index it — every scoped query filters on it

Do all four in **one** migration so there is no window where the column is
nullable in a deployed database.

### Password hashing

The proposal says **bcrypt** (§5.4). Note that `argon2-cffi` is already present
in the environment — but only as a transitive dependency of `minio`, so it must
not be relied on without declaring it.

Argon2id is the stronger modern choice and is what OWASP now recommends. If the
team prefers it, that is defensible — but **declare it explicitly in
`pyproject.toml`** and justify the deviation in the report, exactly as with the
cookie decision. Following the proposal and using bcrypt is the lower-risk
option for marks.

---

## 3. Dependencies

Added to `pyproject.toml`:

```toml
"pyjwt>=2.10,<3",
"bcrypt>=4.2,<5",
"email-validator>=2.2,<3",   # pydantic's EmailStr needs it
```

bcrypt was chosen over argon2id: the proposal names it, and `argon2-cffi` is
only in the tree transitively via `minio`, so depending on it without declaring
it would break the day minio drops it.

---

## 4. Token and cookie

**Claims** — keep them minimal. Every claim is a thing that can go stale:

```json
{ "sub": "<user uuid>", "role": "user", "exp": ..., "iat": ... }
```

**Algorithm:** HS256 is sufficient — one service issues and verifies. The
secret goes in `Settings` as `jwt_secret`, with **no default** so it fails
loudly when unset, and it reaches the container through `docker-compose.yml`
(T-19 — a settings field is not configurable until compose passes it through).

It must also be **at least 32 bytes**. RFC 7518 §3.2 wants an HMAC key at least
as long as the hash output, and PyJWT only *warns* below that — a warning in a
log nobody reads is not a control, so `security.py` refuses instead.

**Lifetime:** 15–30 minutes. Short, because it cannot be revoked. Add a refresh
token only if sessions need to outlive that.

**Cookie:**

```
Set-Cookie: access_token=<jwt>; HttpOnly; SameSite=Strict; Secure; Path=/; Max-Age=1800
```

`SameSite=Strict` rather than `Lax`: nothing links into this app from another
site, so Strict costs nothing and closes CSRF entirely. `Secure` is inert until
TLS lands — set it anyway so it is correct the moment TLS arrives.

---

## 5. Endpoints

### New

| Endpoint | Behaviour |
|---|---|
| `POST /auth/register` | email + password → creates a user, role `user`. 409 on duplicate email. |
| `POST /auth/login` | email + password → `Set-Cookie` with the JWT. **401 with an identical message for both unknown email and wrong password** — distinguishing them tells an attacker which emails exist. |
| `POST /auth/logout` | responds `Set-Cookie: access_token=; Max-Age=0`. Needed because JS cannot clear an `HttpOnly` cookie. |
| `GET /auth/me` | `{ id, email, role }` for the current caller. The frontend needs this because it cannot read the token. |

### Changed

| Endpoint | Change |
|---|---|
| `POST /upload` | requires a caller; sets `owner_id` to that caller |
| `GET /jobs` | scoped to the caller — **in the query**, not filtered after fetching. Filtering post-fetch breaks page sizes, and pagination already exists (P5). |
| `GET /jobs/{id}` | **404, not 403,** for another user's job. A 403 confirms the id exists and lets someone probe for valid ids. |

Put identity resolution in a single FastAPI dependency (`app/api/deps.py`) so
the endpoints stay readable and there is one place to audit.

---

## 6. The operator view — decided

**Option 2: a separate `GET /admin/jobs`, operator-only.** Implemented.

Sprint 1's plan carries an operator story — *"see all jobs and their status, so
I can spot stuck jobs"*. That is precisely the endpoint that must stop being
public. It needs a **role, not deletion**.

Three options:

1. **`GET /jobs` returns all jobs when the caller's role is `operator`.**
   Simplest. Downside: the same URL means different things to different
   callers, which is easy to get wrong and easy to mis-test.
2. **A separate `GET /admin/jobs`, operator-only.** Explicit, separately
   testable, no conditional behaviour on a shared endpoint. More surface.
3. **Drop the operator view for now.** Honest, and the reaper plus its logging
   now covers most of "spot stuck jobs" — that was the story's actual purpose.

**Chosen: option 2.** Conditional behaviour on a shared endpoint is the kind of
thing that passes tests written by the person who built it and surprises
everyone else.

It also turned out to be nearly free: `list_jobs` already returned every
owner's jobs, so the operator route reuses that query unchanged and the
user-facing one gained the scoping. And it gives RBAC something real to gate,
which the proposal asks for as a Must Have -- a role that guards nothing is
hollow evidence.

Option 3 stays defensible if the team later decides nobody wants an operator
screen: the reaper now detects stuck jobs and logs them, which is what that
story existed to serve. An unused privileged endpoint is only attack surface.

---

## 7. Acceptance criteria

- Two users; each sees only their own jobs in `GET /jobs`.
- `GET /jobs/{id}` for another user's job returns **404**.
- Upload assigns the caller as owner.
- The presigned output URL for another user's job is not obtainable through the
  API.
- An unauthenticated request to any job endpoint returns 401.
- Login with a wrong password and login with an unknown email are
  indistinguishable in status and body.
- The cookie is `HttpOnly` — assert on the `Set-Cookie` header, not by hand.
- Logout makes the next request 401.

## 8. Tests

Unit, with a fake user and a pinned secret:

- token round-trip: issue, verify, expiry rejected, tampered signature rejected
- the identity dependency: missing cookie → 401, malformed → 401, valid → user
- cross-owner `get_job` → 404 rather than 403
- `Set-Cookie` carries `HttpOnly` and `SameSite`

Integration (`RUN_POSTGRES_TESTS` guard — T-11):

- the migration applies to a database with existing rows and backfills them
- `list_jobs` scoped by owner returns only that owner's rows — assert
  **membership, not counts**, the table is shared (T-12)
- the `role` CHECK constraint rejects an invalid role

Pin settings explicitly rather than reading the environment (T-13).

---

## 9. Teardown — done

The basic-auth gate existed only because there was no authorization. It was
removed once auth was verified on the deployment -- in that order, so there was
never a window with neither. For the record, what came out:

1. delete `deploy/auth/`
2. remove `include /etc/nginx/app-auth/*.conf;` from `nginx.conf`
3. remove the `./deploy/auth:/etc/nginx/app-auth:ro` mount from
   `docker-compose.yml`
4. keep `/healthz` exempt — it is still the container healthcheck target
5. update `README.md`, `sprint2-backlog.md` (P1) and `CLAUDE.md`

Leaving it is a shared password nobody rotates, in front of a system that no
longer needs one.

---

## 10. Traps that apply

| Trap | Why it applies here |
|---|---|
| [T-11](known-traps.md#t-11) | new integration tests need the `RUN_POSTGRES_TESTS` guard |
| [T-12](known-traps.md#t-12) | the jobs table is shared — assert membership, never counts |
| [T-13](known-traps.md#t-13) | pin the JWT secret in tests; do not read it from the environment |
| [T-19](known-traps.md#t-19) | `JWT_SECRET` must be added to `docker-compose.yml`, not just `Settings` |
