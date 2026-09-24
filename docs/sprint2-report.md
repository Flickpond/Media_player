# Sprint 2 — Team Record

**Written:** 11 September 2026 · figures measured over `86c944e..a6fea7b`,
the sprint's first commit to the one deployed on 11 September
**Sprint window:** 10–11 September 2026
**Status:** all eight work items built, merged, deployed and verified on the
live site.

Companion to [`sprint1-report.md`](sprint1-report.md), which records sprint 1 in
the same form. What to build and why is in
[`sprint2-plan.md`](sprint2-plan.md); this is what happened.

---

## 1. Where the project stands

| | |
|---|---|
| Deployed at | **<https://flickpond.com>** — Alibaba ECS, cn-hongkong |
| Commit live | `a6fea7b`, verified end to end on 11 September |
| Work items | 8 of 8 complete |
| Review findings | P1–P9 all closed |
| Tests passing | **226** (195 Python collected — 170 pass, 25 skipped; 31 frontend — 28 pass, 3 skipped) |
| Sprint 1 baseline | 160 (137 Python, 23 frontend) |
| CI | 5 required checks on `main`, green on every merge |
| Coverage | **85.88%** against a `fail_under = 80` gate |

What sprint 2 changed, in one line each:

* **S2-01** — FFmpeg replaced the copy stand-in. Real 720p H.264 + AAC.
* **S2-02** — a reaper recovers jobs a crashed worker abandoned, and sweeps
  orphaned objects.
* **S2-03** — accounts, per-user ownership, an operator role. **The API was
  serving every user's videos to anyone before this.**
* **S2-04** — CI as a required status check on `main`.
* **S2-05** — TLS, Let's Encrypt, automatic renewal.
* **S2-06** — one place builds MinIO clients, and it still builds two.
* **S2-07** — the bucket is checked once at startup, not on every upload.
* **S2-08** — the error a user reads is no longer the one an operator needs.

---

## 2. Who built what

Figures from `git log 86c944e..a6fea7b`, excluding merge commits and
`package-lock.json` — that is, the eight work items as deployed. The
documentation commits that close the sprint, this report included, are not
counted; they would add two commits to the same author and change nothing else.

| Contributor | Commits | Lines added |
|---|--:|--:|
| Ibrahim Mammadov (@1brahim74) | 22 | 3,954 |

One of those 22 is authored as `Ibrahim Mammadov` via GitHub's web UI; that
address is linked to the same account, so it is not a [T-17](known-traps.md#t-17)
split.
| @ttydw-ch | 0 | 0 |
| @JiangYibai666 | 0 | 0 |
| @zhanj384 | 0 | 0 |
| @Dilute-l | 0 | 0 |

**Sprint 2 was delivered by one person.** That is the single most important
fact in this document, and it is recorded plainly because a contribution record
that obscures it is worthless.

Sprint 1 split five ways and every track delivered. Sprint 2 did not. The work
still had to cross every track's files — FFmpeg needed track D's Dockerfile,
authorization needed track C's models and migrations, TLS needed track D's
nginx config — so ownership was respected by asking rather than by dividing the
labour.

### Where the lines went

| Area | Lines | Share |
|---|--:|--:|
| Tests (`tests/`) | 1,473 | 37% |
| Application code (`app/`) | 929 | 23% |
| Documentation | 793 | 20% |
| Frontend | 322 | 8% |
| Config and other | 234 | 5% |
| CI (`.github/`) | 139 | 3% |
| Deployment (`deploy/`) | 64 | 1% |

Tests outweigh application code roughly 1.6 to 1. That ratio is deliberate, and
section 4 is the argument for it: of the nine defects found this sprint, **six
produced no error of any kind** — they either silently did nothing or silently
did the wrong thing. A test suite is the only thing that catches that class.

### Work items to commits

| Item | Commits |
|---|---|
| S2-04 CI | `5b39637`, `f207705` |
| S2-01 FFmpeg + S2-02 reaper | `d9ff45b`, `88ee6e5`, `87911b3` |
| S2-05 TLS | `0ba68a3`, `2206b33`, `4c61757`, `aaad734`, `353bb68`, `26d456b` |
| S2-03 authorization | `8ae03f2` (design note), `f178ad3`, `28c6e87`, `7382bbd`, `bc1ffe4` |
| S2-06 MinIO clients | `e060160` |
| S2-07 bucket check | `9782618`, `8f5fc20` (revert), `9c81e80` (re-land) |
| S2-08 error hygiene | `2227294` |
| Documentation | `866a31a` (in range), `7338ba9` and this report (after it) |

### Tooling disclosure

**21 of the 22 sprint 2 commits carry a `Co-Authored-By: Claude` trailer.** The
work was done with an AI coding assistant: design decisions, implementation,
tests, review and deployment. The trailers are in the commit history and are
not removable without rewriting it.

This is recorded here because a contribution record that leaves it out is
inaccurate. How it should be presented for assessment is the team's call, not
this document's.

---

## 3. Non-functional requirements: evidence

Verified against the live deployment on 11 September, not against a local
stack.

**N1 — upload returns quickly.** `POST /api/upload` returned `202` with a job
id; the browser is never blocked on processing. S2-07 removed a `bucket_exists`
round trip from every write on this path: 10 uploads went from 10 bucket checks
to **0**.

**N2 — horizontal scaling.** Two worker replicas share one Redis queue and any
replica can take any job. Confirmed live: `media_player-worker-1` and
`media_player-worker-2`, both healthy, and the failure job was picked up by
worker-2.

**N3 — no silent hang.** Every exception path reaches `failed` with a readable
error. Proven live by uploading a deliberately corrupt file — see section 4.

**N4 — single writer.** The worker remains the only writer to `status`,
`output_key`, `error` and `updated_at` after the API's insert. The reaper is the
one exception and uses a conditional UPDATE, so two reapers racing cannot both
claim a row.

**N9 — every transition logged with the job id.** Confirmed in the live worker
log.

**End-to-end, on <https://flickpond.com>:**

```
POST /api/auth/register     201, HttpOnly cookie set
POST /api/upload            202          (1920x1080 source)
status after ~2s            done
output                      h264 1280x720 + aac        <- the downscale ran
presigned URL               200, 64,100 bytes, https
```

**Authorization, verified with two accounts:** each sees only its own jobs;
`GET /jobs/{id}` for another user's job returns **404, not 403**, so the
endpoint cannot be used to probe for valid ids; `/admin/jobs` returns 403 for a
non-operator.

**TLS:** certificate valid to 9 December 2026, renewing automatically, port 80
redirects to 443.

---

## 4. Bug log

Continues sprint 1's numbering. Six of these nine produced **no error message
of any kind**, which is the recurring theme of this project and the reason
[`known-traps.md`](known-traps.md) exists.

### BUG-04 — `list_jobs` ignored its `owner_id` argument · **critical** · fixed `28c6e87`

*Symptom:* none. Every unit test passed. The authorization work looked
complete.

*Cause:* the edit that added the `WHERE owner_id = ...` clause was applied with
a textual replacement that silently did not match, because the target line had
been reformatted. The function kept its new signature and ignored the argument.
The unit tests all monkeypatch `list_jobs`, so none of them touched the real
query.

*Fix:* insert the clause properly. Caught by the CI integration test, which
runs against real PostgreSQL.

*Lesson:* a mechanical edit across several files needs a mechanical check
afterwards, and "ruff passes" is not that check. **P1 would have been reported
closed while still open.**

### BUG-05 — Logout sent no `Set-Cookie` · **major** · fixed `bc1ffe4`

*Symptom:* `POST /auth/logout` returned 204 and the session stayed valid.

*Cause:* the handler mutated FastAPI's injected `Response`, then returned a
**different** `Response` object. FastAPI only merges the injected response's
headers when the handler returns a body for it to serialise, so the
`Set-Cookie` was discarded.

*Fix:* delete the cookie on the object actually returned. The regression test
asserts the clearing header **and its flags**, not just the status code.

### BUG-06 — FFmpeg scale filter rejected · **major** · fixed `d9ff45b`

*Symptom:* `No such filter: 'ih)'` on every transcode.

*Cause:* `scale=-2:min(720,ih)` — FFmpeg splits filter arguments on commas
before evaluating the expression, so `min(720` and `ih)` became two filters.

*Fix:* escape the comma: `scale=-2:min(720\,ih)`. Verified against a real
FFmpeg 7.1.5 rather than assumed.

### BUG-07 — Orphan cleanup was a permanent no-op · **major** · fixed `87911b3`

*Symptom:* the reaper reported success and deleted nothing, forever.

*Cause:* `list_objects` used the SDK's default `recursive=False`, which returns
pseudo-directory entries like `uploads/<uuid>/` with `last_modified=None`. The
reaper skips anything without a timestamp, so it skipped everything.

*Fix:* `recursive=True`. The regression test asserts both halves — a real key
is returned, and it has a usable timestamp.

### BUG-08 — The reaper logged nothing · **moderate** · fixed `88ee6e5`

*Symptom:* a running reaper produced no output at all.

*Cause:* `getLogger` without `basicConfig`. **This is BUG-02 from sprint 1
recurring in a new component** — now recorded as [T-18](known-traps.md#t-18).

*Fix:* call `configure_logging("INFO")` at start-up.

### BUG-09 — nginx 401 for anonymous, 500 for authenticated · **moderate** · fixed during S2-03 deployment

*Cause:* `chmod 600` on the htpasswd file. nginx workers run as `nginx`, not
root, so the master could start but the workers could not read the file.

*Fix:* permissions the worker user can read. Recorded as
[T-07](known-traps.md#t-07).

### BUG-10 — nginx healthcheck failed on a correct config · **minor** · fixed during S2-05

*Cause:* the healthcheck used `localhost`, which resolved to `::1`, but the
read-only configuration mount blocked the image's IPv6 patch.

*Fix:* use `127.0.0.1`. Recorded as [T-08](known-traps.md#t-08).

### BUG-11 — `trivy-action@0.28.0` does not exist · **minor** · fixed `f207705`

*Cause:* a version pinned from memory rather than from the tag list. CI failed
on a step that had never run.

*Fix:* `v0.36.0`.

### BUG-12 — A deploy silently halved the worker pool · **moderate** · open

*Symptom:* after a routine `docker compose up -d --build`, every container
healthy, no warning, and **one** worker instead of two.

*Cause:* the second replica came from `--scale worker=2` typed on the command
line in an earlier deploy. It was never in `docker-compose.yml`, so it existed
only in one machine's shell history — invisible to code review and to CI.

*Status:* worked around by passing `--scale worker=2` on every deploy, and
recorded as [T-23](known-traps.md#t-23). The durable fix is `deploy.replicas: 2`
in `docker-compose.yml`, which is **track D's file** and has not been changed.

---

## 5. Known limitations

Carried forward, still true:

* **Upload is not atomic.** The object lands in MinIO before the job row is
  committed, so a crash between the two leaves an orphaned object. The reaper
  now sweeps them, which bounds the problem rather than removing it.
* **A JWT cannot be revoked.** Logout clears the browser's copy; the token
  itself stays valid until it expires. The short lifetime is the real bound.
  A Redis denylist would fix it and would give up the statelessness that
  justified JWT in the first place.
* **Content sniffing proves the container, not the stream.** A file with a
  valid `ftyp` header followed by garbage is accepted, queued, and fails in
  FFmpeg. That is by design — and it is what section 3's failure test used.
* **The reaper's lease is a timeout, not a heartbeat.** A genuinely slow job can
  be reaped mid-flight. The FFmpeg timeout (870 s) sits below the RQ job
  timeout (900 s), which sits below the lease (1800 s), which makes it unlikely
  rather than impossible.
* **Dev credentials remain in `docker-compose.yml`.** Tolerable only because
  every datastore is loopback-bound.

---

## 6. What is left

- [ ] **`deploy.replicas: 2` in `docker-compose.yml`** (BUG-12 / T-23). Track
      D's file. Until then every deploy needs `--scale worker=2`.
- [ ] **Four teammates contributed nothing to sprint 2.** This is a team
      matter, not a technical one, but it belongs in the record.
- [ ] **Two files still fail `ruff format --check`** — `app/services/storage.py`
      and `tests/integration/test_storage.py`, both pre-existing and both track
      B's. The project lints with `ruff check`, not `ruff format`, so this is
      cosmetic.
- [ ] **`POST /upload` returns `job_id` where `GET /jobs/{id}` returns `id`.**
      Same entity, two names. Not a bug; changing it would break the frontend.
      Worth settling before a third sprint builds on it.
- [ ] **23 `Agent host session ... turn N` checkpoint commits** exist on a side
      branch. They are not on `main` and do not affect its history, but the
      branch is clutter and can be deleted.

---

## Revision log

| Date | Author | What changed |
|---|---|---|
| 11 Sep 2026 | @1brahim74 | Initial record: contributions, NFR evidence, BUG-04 to BUG-12, limitations, outstanding items. |

### How to update this

- **Contributions** — `git log --no-merges --format='@%an' --numstat <sprint-start>..main -- . ':(exclude)*package-lock.json'`
- **New bugs** — add a `BUG-0n` entry to section 4 in the same
  symptom / cause / fix form. Numbering continues across sprints and an id is
  never reused.
- **NFR evidence** — paste real command output, against the deployment, rather
  than describing it.
