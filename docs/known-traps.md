# Known traps

Every one of these was hit for real on this project. They are recorded because
each cost time, and because most of them **look like they are working** right up
until they don't. Read the relevant entry before changing the area it touches.

Sprint 1's three functional bugs (BUG-01 to BUG-03) and ENV-01 are in
[`sprint1-report.md`](sprint1-report.md) §4. This file covers the traps found
during the 7 September security review and the 9 September deployment, plus the
testing and tooling traps that surfaced alongside them.

**How to use this file:** find the area you are about to touch in the table,
read that entry, then work. If you hit something new, add an entry.

| # | Area | One line |
|---|---|---|
| [T-01](#t-01) | Upload | The client's `Content-Type` is attacker-controlled |
| [T-02](#t-02) | Upload | A renamed file defeats any header-based check |
| [T-03](#t-03) | Upload | The filename goes into an object key |
| [T-04](#t-04) | Upload | The size check runs after the body is already on disk |
| [T-05](#t-05) | Deploy | Datastores on `0.0.0.0` with one firewall in front |
| [T-06](#t-06) | Deploy | `ufw` does not protect Docker-published ports |
| [T-07](#t-07) | Deploy | nginx auth file permissions fail only when authenticated |
| [T-08](#t-08) | Deploy | Read-only `nginx.conf` disables the image's IPv6 patch |
| [T-09](#t-09) | Frontend | Deciding the API base from `location.port` |
| [T-10](#t-10) | Config | Redis password not URL-encoded |
| [T-11](#t-11) | Tests | Integration tests that assume services are up |
| [T-12](#t-12) | Tests | Tests that assume an empty database |
| [T-13](#t-13) | Tests | Tests that read ambient environment variables |
| [T-14](#t-14) | Repo | `.gitignore` silently swallowing files you meant to commit |
| [T-15](#t-15) | Repo | CRLF shell scripts fail on Linux |
| [T-16](#t-16) | Repo | Private keys in the working directory |
| [T-17](#t-17) | Repo | Git identity mismatch splits contribution history |
| [T-18](#t-18) | Logging | A logger with no `basicConfig` writes nothing |
| [T-19](#t-19) | Config | A settings field is not configurable until compose passes it through |
| [T-20](#t-20) | Deploy | `git reset --hard origin/main` without fetching deploys stale code |
| [T-21](#t-21) | Deploy | `docker compose up -d` applies env changes but not code changes |
| [T-22](#t-22) | Errors | An exception message is not a user message |
| [T-23](#t-23) | Deploy | `docker compose up` silently resets a `--scale` replica count |

---

## Security and upload

### T-01

**The client's `Content-Type` is attacker-controlled.**

*Symptom:* an uploaded HTML file is stored and later served back as `text/html`
from the object store's origin — stored XSS.

*Cause:* `file.content_type` comes from the multipart headers. The browser's
`accept="video/*"` is a file-picker filter, not a control.

*Fix (already in place):* `ALLOWED_CONTENT_TYPES` in `app/api/uploads.py`
rejects anything not on the list with 415, **and** the stored type is the
sniffed one, never the declared one. Presigned URLs also carry
`Content-Disposition: attachment`.

*Avoid:* never store or echo a client-supplied content type. Derive it.

### T-02

**A renamed file defeats any header-based check.**

*Symptom:* `doc.pdf` renamed to `doc.mp4` uploads successfully.

*Cause:* browsers derive `Content-Type` from the **file extension**, so a
renamed PDF genuinely arrives declared `video/mp4`. An allowlist on the declared
type cannot see this.

*Fix (already in place):* `app/services/media_type.py` reads the container
signature from the first 4 KB. `sniff_video_type()` returns the real type or
`None`, and `None` means 415.

*Avoid:* validate bytes, not names or headers. Note the current limit: sniffing
proves the **container**, not that the stream decodes. A valid `ftyp` header
followed by garbage is still accepted — FFmpeg (S2-01) is what will catch that.

### T-03

**The filename goes into an object key.**

*Symptom:* a filename of `../../outputs/x.mp4` lands in `source_key`.

*Cause:* the key was built as `uploads/{job_id}/{filename}` with a raw client
value.

*Fix (already in place):* `safe_filename()` in `app/api/uploads.py` — last path
segment only, charset filter, length cap. The original is kept on the row for
display.

*Avoid:* S3 keys are opaque strings, so this was not a live traversal — but the
invariant "everything for job X lives under `uploads/X/`" must be enforced in
our code, not left to whatever the storage backend happens to normalise.

### T-04

**The size check runs after the body is already on disk.**

*Symptom:* a 50 GB upload fills the container's disk and *then* gets its error.

*Cause:* Starlette parses the whole multipart body into a `SpooledTemporaryFile`
during dependency resolution — before the endpoint function runs. A `seek`/`tell`
inside the handler measures bytes that have already landed.

*Fix (already in place):* a `Content-Length` guard in `app/main.py` middleware
returns 413 before the body is read. The handler check remains as a backstop for
a request that understates its length.

*Avoid:* any limit enforced inside a FastAPI handler is a backstop, never a
first line of defence.

---

## Deployment and infrastructure

### T-05

**Datastores on `0.0.0.0` with one firewall in front.**

*Symptom:* PostgreSQL, Redis and the MinIO console listening on all interfaces
on a public host.

*Cause:* `*_BIND` environment variables set to `0.0.0.0` for a cloud demo, with
the cloud security group as the only thing keeping them private.

*Fix (already in place):* those variables are **gone**. `docker-compose.yml`
pins every service except nginx to `127.0.0.1` and that is not configurable.
Use an SSH tunnel:

```bash
ssh -L 5432:127.0.0.1:5432 root@<host>
ssh -L 9001:127.0.0.1:9001 root@<host>
```

*Avoid:* do not reintroduce a bind variable for a datastore. If you think you
need one, you need a tunnel.

### T-06

**`ufw` does not protect Docker-published ports.**

*Symptom:* `ufw status` shows only one allowed port while several services are
reachable from the internet.

*Cause:* Docker inserts its own rules into the `DOCKER` iptables chain, which is
evaluated **before** ufw's `INPUT` rules. Published container ports bypass ufw
entirely.

*Avoid:* never conclude a host is firewalled from `ufw status`. Check what is
actually listening with `ss -tlnp`, and test from outside the host.

Useful diagnostic: a firewall that **drops** packets gives you a timeout, while
an allowed port with nothing listening gives a fast connection-refused. Same
failure to the eye, different cause — and it lets you test whether a port is
open before you move a service onto it.

### T-07

**nginx auth file permissions fail only when authenticated.**

*Symptom:* anonymous requests get a correct `401`; correct credentials get `500`.

*Cause:* nginx runs its **master** as root but drops **workers** to the `nginx`
user, and a worker is what opens `auth_basic_user_file`. A `chmod 600` file is
unreadable to it. Anonymous requests never reach the file read, so they look
fine — the gate appears healthy until someone tries to log in.

*Fix (already in place):* `deploy/auth/set-password.sh` always writes mode 644,
and `deploy/auth/verify.sh` detects this exact case and prints the diagnosis.

*Avoid:* never hand-write `deploy/auth/htpasswd`. Run the script. If you see a
500 from an authenticated request, check
`docker logs <frontend> 2>&1 | grep 'Permission denied'` before anything else.

### T-08

**Read-only `nginx.conf` disables the image's IPv6 patch.**

*Symptom:* the container is marked `unhealthy` while serving every real request
correctly.

*Cause:* the `nginx:alpine` entrypoint script
`10-listen-on-ipv6-by-default.sh` appends `listen [::]:80;` to the config. We
mount `nginx.conf` read-only, so it cannot — it logs
`can not modify /etc/nginx/conf.d/default.conf (read-only file system?)` and
continues. nginx then binds IPv4 only, while `/etc/hosts` still maps `localhost`
to `::1` as well. A healthcheck using `localhost` tries IPv6 first and gets
refused.

*Fix (already in place):* the healthcheck uses `http://127.0.0.1/healthz`.

*Avoid:* inside containers, prefer `127.0.0.1` over `localhost` in healthchecks.
An unhealthy frontend is not cosmetic — compose gates other services on it.

---

## Frontend

### T-09

**Deciding the API base from `location.port`.**

*Symptom:* the app works on `:3000` and breaks on any real deployment.

*Cause:* a check of the form
`window.location.port === "3000" ? "/api" : "http://127.0.0.1:8000"`.
On ports 80 and 443 — every real deployment — `location.port` is the empty
string, so the fallback fires and the browser calls **the visitor's own
machine**.

*Fix (already in place):* `const API = "/api"`. nginx serves the page and
proxies `/api/`, so a relative base is correct everywhere.

*Avoid:* never branch on the port. If the page and the API share an origin, use
a relative path unconditionally.

---

## Configuration

### T-10

**Redis password not URL-encoded.**

*Symptom:* a password containing `@` or `/` silently points the client at a
different host instead of failing loudly.

*Cause:* the password is interpolated into the userinfo part of a URL.

*Fix (already in place):* `quote(self.redis_password, safe="")` in
`app/config.py`.

*Avoid:* percent-encode anything you interpolate into a URL.

---

## Testing

### T-11

**Integration tests that assume services are up.**

*Symptom:* the suite passes on your machine and fails everywhere else,
especially in CI.

*Cause:* `tests/integration/test_storage.py` had no skip guard and hit MinIO
unconditionally. It only passed because a stack happened to be running.

*Fix (already in place):* it now honours `RUN_POSTGRES_TESTS` like its siblings.

*Rule:* **everything under `tests/integration/` must carry the guard.**

```python
pytestmark = pytest.mark.skipif(
    os.getenv("RUN_POSTGRES_TESTS") != "1",
    reason="set RUN_POSTGRES_TESTS=1 to run tests that need the live compose stack",
)
```

### T-12

**Tests that assume an empty database.**

*Symptom:* a test passes alone and fails in the full run.

*Cause:* a pagination test asserted exact page sizes, assuming its five rows
were the only ones in the table. The database is shared with the live stack.

*Avoid:* never assert on absolute counts or exact page contents against a shared
table. Create rows with known ids, page through everything, assert *your* ids
appear exactly once, and clean up in a `finally`.

### T-13

**Tests that read ambient environment variables.**

*Symptom:* a test passes or fails depending on whether the developer has a
configured `.env`.

*Cause:* `Settings` loads `.env`, so a test calling `get_settings()` picks up
whatever the developer has. When `.env.example` gained `REDIS_PASSWORD`, a test
asserting a password-less Redis URL started failing.

*Avoid:* pin settings explicitly rather than reading the environment.

```python
pinned = Settings(redis_host="127.0.0.1", redis_port=6379, redis_password="", redis_ssl=False)
monkeypatch.setattr(module, "get_settings", lambda: pinned)
```

---

## Repository and tooling

### T-14

**`.gitignore` silently swallowing files you meant to commit.**

*Symptom:* a commit succeeds and ships documentation for scripts that are not in
the repository.

*Cause:* `deploy/auth/*` excluded everything in the directory; only
`!deploy/auth/README.md` was re-included. `git add -A` **skips ignored files
without any warning**.

*Fix (already in place):* `!deploy/auth/*.sh` alongside the README negation.

*Avoid:* after adding an ignore rule, verify both directions —
`git check-ignore -v <file>` for what should be ignored, and
`git ls-files <dir>` for what should be tracked. Note that `!` cannot rescue a
file if the **parent directory** is excluded: `deploy/auth/` and
`deploy/auth/*` behave differently, because git never descends into an excluded
directory.

### T-15

**CRLF shell scripts fail on Linux.**

*Symptom:* `bad interpreter: /bin/sh^M: No such file or directory`.

*Cause:* a script authored on Windows checked out with CRLF endings.

*Fix (already in place):* `.gitattributes` pins `*.sh`, `Dockerfile`, `*.yml`
and `nginx.conf` to `eol=lf`.

*Avoid:* add any new executable or container-parsed file type to
`.gitattributes`. Verify with `head -1 script.sh | cat -A` — you want
`#!/bin/sh$`, not `#!/bin/sh^M$`.

### T-16

**Private keys in the working directory.**

*Symptom:* a server's root private key sits in the repo folder, untracked but
**not ignored** — one `git add -A` from being pushed to a public repository.

*Fix (already in place):* `*.pem`, `*.key`, `*.p12`, `*.pfx`, `id_rsa*`, `*.ppk`
are ignored repo-wide.

*Avoid:* keep keys in `~/.ssh/`, not the project. A key that reaches a public
repo even once is compromised permanently — deleting it later does not help,
because unreferenced objects survive and forks keep their own copies. The only
real fix is rotating the key on the server.

### T-17

**Git identity mismatch splits contribution history.**

*Symptom:* commits do not appear in a contributor's GitHub history.

*Cause:* a local `user.name`/`user.email` that matches no GitHub account.

*Fix (already applied globally):* `1brahim74 <ibrahimmemmedov9a@gmail.com>`.

*Avoid:* before committing on a new machine, run
`git log --format='%an <%ae>' | sort -u` and confirm your identity matches one
already in the history.

---

## Logging and configuration

### T-18

**A logger with no `basicConfig` writes nothing. This has now happened twice.**

*Symptom:* a component runs correctly and produces no log output at all.

*Cause:* `logging.getLogger(...)` creates a logger; it does not configure
logging. Without a `basicConfig` call somewhere in the process, the root logger
has no handler and no level, so `logger.info()` is discarded outright and
`logger.warning()` falls through to Python's `lastResort` handler -- stderr,
bare message, no timestamp and no logger name.

*History:* this is **BUG-02 from sprint 1** ("worker silently ignored
`--log-level`", see [`sprint1-report.md`](sprint1-report.md) §4) recurring in
`app/worker/reaper.py`. Two sprints, two entry points, one cause. The reaper
version was worse: it fails users' jobs and deletes their objects, and it did
both in total silence in production before anyone noticed.

*Fix (already in place):* every entry point calls `configure_logging()` from
`app/worker/__main__.py`, which uses `force=True` -- necessary because RQ
installs its own handlers before our code runs, and `basicConfig` is a no-op
once handlers exist.

*Avoid:* **a new `python -m` entry point needs a `configure_logging()` call in
its `main()`.** Creating the logger is not the same as turning it on. Verify by
running the thing and looking for output -- a component that logs nothing looks
identical to a component that is working perfectly.

### T-19

**A settings field is not configurable until compose passes it through.**

*Symptom:* you add a field to `Settings`, set the matching variable in `.env`,
restart, and the container still uses the default.

*Cause:* compose's `.env` supplies variables for **substitution inside
`docker-compose.yml`**, not to the container's environment. Our services
declare explicit `environment:` blocks, so a variable the compose file never
mentions never reaches the process. `pydantic-settings` then falls back to the
default, silently and correctly.

*How it surfaced:* all ten settings added in sprint 2 -- the five
`WORKER_FFMPEG_*`, `WORKER_JOB_TIMEOUT_SECONDS`, `WORKER_OUTPUT_PREFIX`, and
the three `REAPER_*` -- were unreachable in the deployment. `REAPER_LEASE_SECONDS`
was added specifically so the crash-recovery demo would not take 30 minutes,
and setting it in `.env` changed nothing.

*Fix (already in place):* the pass-throughs are in `docker-compose.yml`, in the
form `REAPER_LEASE_SECONDS: ${REAPER_LEASE_SECONDS:-1800}`, which keeps the
default in one place while making it settable.

*Avoid:* adding a field to `app/config.py` is half the change. Add the
pass-through in the same commit, and check it with:

```bash
docker compose config --format json | python3 -c "import json,sys;   print(sorted(json.load(sys.stdin)['services']['worker']['environment']))"
```

`WORKER_FFMPEG_BINARY` is deliberately excluded -- it decides *what executes*
rather than how, and the binary is a property of the image.

### T-20

**`git reset --hard origin/main` without fetching first deploys stale code.**

*Symptom:* a deploy reports success and runs a commit from an hour ago.

*Cause:* `origin/main` is a **remote-tracking ref** — a local cache of what this
machine last heard from GitHub. Without `git fetch`, resetting to it resets to
whatever it saw last, and git does exactly what you asked with no warning.

*Avoid:* always fetch first, and **print the commit after deploying**:

```bash
git fetch origin && git reset --hard origin/main
git log --oneline -1        # confirm it is what you expected
```

This was caught only because the deploy printed the commit. Without that line
it would have looked like a successful deploy of code that was never there.

### T-21

**`docker compose up -d` applies environment changes but not code changes.**

*Symptom:* a new setting is present in the container's environment, `docker
compose config` shows it, and the application still behaves as though it does
not exist.

*Cause:* compose recreates a container when its *configuration* changes, but it
does not rebuild the **image**. Source code lives in the image. So an `.env`
edit takes effect immediately while the code that reads it is still the version
baked in at the last build.

*How it surfaced:* `MINIO_PUBLIC_USE_SSL=true` was correct in `.env`, correct in
`docker compose config`, and correct in `env` inside the running container --
but `Settings` raised `AttributeError: no attribute 'minio_public_use_ssl'`,
because the image predated the field. Presigned URLs kept coming out as `http://`.

*Avoid:* **`docker compose up -d --build`** whenever the deployed commit changed.
Verify by asking the application, not the environment:

```bash
docker compose exec api python -c "from app.config import get_settings; print(get_settings().some_new_field)"
```

Reading `env` inside the container proves the variable arrived. It does not
prove the code that consumes it exists.

---

### T-22

**An exception message is not a user message, and one string cannot be both.**

*Symptom:* a user whose upload failed is shown
`ObjectStoreError: download failed for uploads/9f3a/annas-wedding-speech.mov:
AccessDenied`, or four hundred characters of FFmpeg stderr naming a `/tmp` path
inside a container they have never heard of.

*Cause:* `readable_error()` built the `error` column from `str(exception)`,
prefixed with the class name for anything that was not an `ObjectStoreError`.
That column is returned verbatim by `GET /jobs/{id}`. The messages were written
by whoever raised them, for whoever would read the log -- a completely different
audience, with completely different needs, and nothing in the type system said
so.

*Why it survived review for a sprint:* every message was genuinely useful. Each
one was written carefully, by someone imagining an operator reading it. The
defect was not a bad message, it was a missing distinction, and a missing
distinction has nothing to point at in a diff.

*Avoid:* carry both, and make both mandatory.

```python
raise ObjectStoreError(
    f"download failed for {key}: {exc.code}",                  # log
    user_message="the uploaded file could not be read back",   # screen
) from exc
```

`user_message` is a **required** keyword argument, not an optional one with a
safe default. A default would mean a forgetful raise site silently degrades to a
vague message; required means it fails in CI. On a codebase where most traps in
this file fail silently, prefer the one that shouts.

*The test that holds it:* `test_no_storage_failure_puts_an_object_key_in_the_users_half`
sweeps every raise site in `MinioObjectStore` and asserts **both** halves -- the
key present in `str(exc)`, absent from `user_message`. Asserting only the second
would pass for a "fix" that deleted the key entirely and left operators with
nothing to debug from.

---

### T-23

**`docker compose up` silently resets a replica count that was set with
`--scale`.**

*Symptom:* the deployment has run two workers for weeks. A routine
`docker compose up -d --build` reports success, every container is healthy, and
there is now **one** worker. Nothing warns you, and throughput halves.

*Cause:* `--scale worker=2` is an argument to a single `up` invocation, not
persisted state. Compose reconciles the running stack against the compose file
plus *this* command's flags; with no replica count in either, the default of 1
wins and the extra container is removed as surplus.

*Why it hides:* the missing replica is not an error, a warning, or an unhealthy
container — it is an *absence*. `docker compose ps` looks entirely normal unless
you are counting rows, and the queue still drains, just half as fast. The
setting existed only in shell history on one machine, so it was invisible to
code review and to CI.

*How it surfaced:* the sprint 2 deployment. Comparing `docker compose ps` before
and after showed two `worker` rows become one; `grep "docker compose up"
/root/.bash_history` found the original `--scale worker=2` that nothing in the
repository recorded.

*Avoid:* until the count lives in `docker-compose.yml`, pass it every time:

```bash
docker compose up -d --build --scale worker=2
```

The durable fix is to put it in the compose file, so a plain `up -d --build`
reproduces the intended topology:

```yaml
worker:
  deploy:
    replicas: 2
```

That file is track D's. **Any deployment topology that lives only in a command
someone once typed will be lost** — this is the same family as T-20 and T-21:
the running system quietly differs from the one everybody describes.
