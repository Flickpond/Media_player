# Sprint 5 — Plan

**Window:** Monday 12 October – Sunday 18 October 2026
**Freeze:** Friday 16 · **Cutover and verify:** Saturday 17 · **Report:** Sunday 18
**Written against:** `main` @ `473f117` · from [`roadmap.md`](roadmap.md) and
[`sprint4-report.md`](sprint4-report.md)

**Sprint goal:** flickpond.com runs on AWS — a small always-on core, managed
Postgres and Redis, and spot workers that scale with the queue — and plays a
phone's 4K video in 4K. Edits start from the original upload, with a crop box
measured in its pixels and a clip scrubber on its timeline.

One member, one function, as in sprints 3 and 4. Weekends count as working
days. **Read [`known-traps.md`](known-traps.md) before writing code.**

---

## 0. Who has what

| Track | Owner | Function | Main deliverable |
|---|---|---|---|
| **A** | Ibrahim Mammadov (@1brahim74) | Processing and AWS | RDS and ElastiCache in the base; workers that survive being stopped; the cutover |
| **B** | Zhang Jizhang (@zhanj384) | Upload path and accounts | Direct upload proven on S3; one upload path instead of two; deleting a user |
| **C** | Yang Dongwei (@ttydw-ch) | Media rules and the clip scrubber | Editing from the original upload; the clip scrubber |
| **D** | Jiang Yibai (@JiangYibai666) | Infrastructure | The worker Auto Scaling Group live on spot; images from CI; the login fix |
| **E** | Lu Jingxing (@Dilute-l) | The UI | Crop box in the original's pixels; cancel and faster uploads (#67, #68) |

**A and D carry the cutover between them.** If either slips, Saturday stays
on the current server — it already runs everything sprint 4 shipped.

---

## 1. Decisions already made

- **Production moves to AWS this sprint, onto the split design** — not onto
  one large host first. Staging proved 4K on AWS in sprint 4 (a 20-second 4K
  clip fully laddered in 41 s); this sprint makes it the real site.
- **The shape, and what it costs** (Singapore prices, 8 October):

  | Part | What | Per month |
  |---|---|---|
  | Core | `t4g.small`: nginx, API, reaper | ≈ $15 |
  | Database | **RDS PostgreSQL `db.t4g.micro`**, 20 GB, private, TLS enforced | ≈ $21 |
  | Queue | **ElastiCache Valkey `cache.t4g.micro`**, TLS and an auth token | ≈ $14 |
  | Workers | spot `c7g.xlarge`, 0…N on queue depth, one kept during the day | ≈ $11–33 |
  | Disks, address | | ≈ $7 |
  | **Total** | | **≈ $70–90** |

  One `c7g.2xlarge` running everything would be ≈ $250. Video traffic out of
  S3 (≈ $0.12/GB) is on top either way.
- **Managed datastores, not Postgres and Redis on the core.** RDS brings
  automatic backups and point-in-time restore. ElastiCache can never be given
  a public address — which matters, because anything that can write to the
  RQ queue can make a worker run code of its choosing.
- **Opening the datastores to other machines is reviewed before it is
  applied.** A one-page note — ports, which security groups, TLS and
  passwords, and the test that proves both are unreachable from outside
  AWS — read by someone other than whoever runs `apply`.
- **A stopped worker gives its job back.** Spot reclaims and scale-in are
  routine now; a job interrupted by either goes back on the queue instead of
  failing 40 minutes later.
- **The ladder cap is 2160p on the workers, the MP4 stays ≤ 1080p.** As
  sprint 4 decided.
- **4K costs less to encode** (Track A, landing before the sprint starts):
  every rung capped at **30 fps** (a 60 fps phone video costs half);
  **480p only when it is the video's top rung** — 1440p stays; the lower
  rungs scaled down **from the 1080p rendition** instead of from 4K; the
  audio encoded **once** and shared by every rung. Measured on staging
  before and after with the same 4K phone clip; both numbers go in the PR.
  This is what lets a 4-vCPU spot worker carry 4K comfortably.
- **"Keep 60 fps" is the uploader's choice** (#71, with the item above). Off
  by default; ticked, that video keeps 60 fps in the MP4, every rung and any
  edit made from it. Stored as `max_fps` on the job.
- **An operator can delete a user** (2.4), with everything they uploaded. A
  full admin page with real roles and rights is sprint 6.

---

## 2. The contracts — agree these on Monday

### 2.1 What the base exposes (A builds it, D's worker module reads it)

New outputs of `infra/terraform`, alongside sprint 4's:

```
core_private_ip            the core host, for anything workers still reach on it
db_endpoint, db_port       RDS PostgreSQL
db_security_group_id       admits the core and the worker group only
cache_endpoint, cache_port ElastiCache Valkey (TLS)
cache_security_group_id    admits the core and the worker group only
db_secret_parameter        SSM parameter holding the DSN, never a Terraform output
cache_secret_parameter     SSM parameter holding the auth token
```

D's datastore rules point at `db_security_group_id` and
`cache_security_group_id` instead of the core's security group. Secrets live
in SSM Parameter Store under `/flickpond/<environment>/`, which D's worker
role can already read; they never appear in Terraform output or state output.

### 2.2 Cropping and clipping the original (C builds the API and the scrubber, E the crop box)

```
POST /jobs/{id}/edit
  { "operations": [ { "operation": "crop", "params": { "x", "y", "w", "h" } },
                    { "operation": "clip", "params": { "start", "end" } }, ... ] }
```

- An edit **always starts from the original upload**, even when `{id}` is
  itself an edit: the API follows the chain to the root job and uses its
  `source_key`. Edits of edits no longer lose quality at each step.
- Crop coordinates are in **the original's pixels** — the `width` and
  `height` on the root job's row. The page scales the box from what it shows
  on screen to those numbers before sending.
- `GET /jobs/{id}` gains `edit_source: { "job_id", "width", "height",
  "duration_seconds" }`, so the page knows which video it is really editing
  without walking the chain itself.
- **The editor plays the `edit_source` video**, not the one the reader
  clicked. Then the crop box and the scrubber both measure the original
  directly: crop in its pixels, clip `start` and `end` in seconds on its
  timeline. Without this, clipping an edit that was itself a 10-second
  excerpt would cut the original at the excerpt's times.
- A crop outside the original's frame, or a clip past its end, stays a
  **worker** failure with a readable error (sprint 3's rule); the API checks
  only the shape of the request.
- **The clip scrubber** mounts where the editor already expects it,
  `window.mountClipScrubber(video, onChange)` (see
  `frontend/EDITOR-COMPONENTS.md`), and reports `{ start, end }` in seconds:
  two handles over the timeline, the range between them highlighted, a
  `m:ss` readout, the player seeking as a handle moves, arrow keys on a
  focused handle, and `start < end` impossible to break by dragging.

### 2.3 Stopping a worker (D sends it, A handles it)

- On a spot interruption notice or a scale-in, the instance runs
  `docker stop --time 100` on its worker containers. Docker sends `SIGTERM`,
  then kills after 100 s — inside AWS's two-minute warning.
- The worker, on `SIGTERM`: stops FFmpeg, clears the job's partial output, and
  puts the job back — `processing → queued`, or the ladder back on its queue
  with `hls_status` still `pending`. After **three** interruptions the job
  fails with a readable error instead of looping.
- The reaper stays the safety net for a worker that dies with no warning; its
  lease drops from 40 minutes to a few minutes past the job timeout, once
  workers send RQ heartbeats.

### 2.4 Deleting a user (B builds it, E adds the button)

```
DELETE /admin/users/{id}
  204                                       // the user and everything they own
  403 { "error": "operator role required" }
  404 { "error": "not found" }
  409 { "error": "an operator cannot delete their own account" }
```

- **Everything goes, storage first:** every job's source, MP4, ladder and
  thumbnail through the same cleanup `DELETE /jobs/{id}` already uses; open
  upload sessions aborted; then the jobs, the sessions and the user row in
  one transaction. A storage failure stops the delete before any row goes,
  so nothing is left pointing at nothing.
- **Their session ends at once:** the API already loads the user on every
  request, so a deleted user's cookie gets 401 on the next call rather than
  living out its 30 minutes.
- **No deleting yourself** — the 409 is what stops the last operator from
  locking everyone out.
- The admin panel lists users with a **Delete** button and a confirmation
  that names the account and how many videos go with it.

---

## 3. The one pairing rule, again

**Editing from the original (C) and the crop box (E) ship together, or
neither ships** — the same rule as sprint 4, where both were a no-go. The
crop rectangle only means something in the original's pixels once edits
start from the original. **Decide on Wednesday 14.**

**The clip scrubber is not bound by this rule.** It measures whatever video
the editor plays — the original if the pair ships, the clicked video if it
doesn't — and the edit runs on that same video either way, so it ships on
its own.

---

## 4. Schedule

**How each day ends:** open or update your PR by end of day; Ibrahim reviews
and merges that evening. Small daily PRs, not one branch that lands on Friday.

| | Mon 12 | Tue 13 | Wed 14 | Thu 15 | **Fri 16 · freeze** | **Sat 17 · cutover** | Sun 18 |
|---|---|---|---|---|---|---|---|
| **A** Ibrahim | Terraform: RDS, ElastiCache, their security groups, SSM secrets, the outputs in 2.1; remote state with locking. Post contracts 2.1 and 2.3. | Worker `SIGTERM` handling: give the job back, attempt cap of 3, clear partial output. **Post the datastore review note.** | Apply the datastores on staging once the note is read; core shrinks to `t4g.small`. RQ heartbeats; shorter reaper lease. Rewrite CLAUDE.md rule 2. | Time a real 4K phone clip through a spot worker. Rehearse the data move: restore a copy of production's dump into staging's RDS. Update the cutover runbook for RDS. | PR complete. **Lower flickpond.com's DNS TTL to 300 s at Spaceship** (Ibrahim holds the login). Budget alert on the AWS account at $80 a month. | **12:00 decision.** Cutover: stop writes, `pg_dump` → RDS, MinIO → S3 (465 MB), certificates, **the A record at Spaceship**. Verify a 4K upload plays in 4K. | Sprint report. CLAUDE.md's deployment section rewritten for AWS. |
| **B** Zhang | Cap open upload sessions per user. Agree 2.4 with E. Plan retiring `POST /upload` with E: what the fallback still covers. | Direct upload on staging S3: CORS from the staging origin, `ETag` exposed, a resumed upload. | `DELETE /admin/users/{id}` (2.4): storage first, then rows in one transaction; tests for the 403, 404 and 409. | Files the browser can't type go through the direct path too (the content sniff on `complete` already identifies them); `POST /upload` then retires. A 1 GB upload on staging, resumed once. | PR complete | Verify a 1 GB upload on production | Slack |
| **C** Yang | Agree contract 2.2 with E. Design: finding the root job; what `edit_source` returns for uploads, edits and edits of edits. Clip scrubber: two handles over the timeline, reporting `{start, end}` through `mountClipScrubber`. | Edit from the original: the API follows the chain; `edit_source` in the API. Scrubber: highlighted range, `m:ss` readout, the player seeks as a handle moves. | Tests across the edit path, including an edit of an edit. Scrubber: arrow keys, `start < end` unbreakable, proportional on a 10-second and a 10-minute video. **Go / no-go** with E. | Verify clip — and, if go, crop and scale — on staging against a 4K original. Raise the duration limit if A's 4K timing allows it. | PR complete | Verify on production | Slack |
| **D** Yibai | Apply `infra/registry`; set the repository variables so CI pushes a worker image per merge. Repoint the module's datastore rules at 2.1's groups. | `terraform plan` the worker group against staging with the datastores open; read A's review note. | Apply the worker group on staging: spot, queue-depth scaling, scheduled minimum. Spot interruption → `docker stop` (2.3). | Interruption test with A (AWS FIS, or `docker stop` mid-encode). The 5.8 s login fix if the investigation found one; DAST against staging. | PR complete. A health check on `https://flickpond.com/api/health` with an email alarm, ready to arm at the cutover. | Cutover with A: workers live on production; arm the health alarm; watch the scaling alarms. | Write-up: the first week's real AWS cost |
| **E** Lu | Agree contract 2.2 with C. Cancel an upload in progress (#67). | The editor plays the `edit_source` video. Crop box drawn over it, converted to its pixels. | Upload parts three at a time, measured before and after (#68). **Go / no-go** with C. | Quality labels up to 2160p, with "60 fps" on videos that kept it; a "starting a worker…" note when a job waits on a cold start. The **Delete user** button in the admin panel (2.4). One resume note per upload (#69) if time allows. | PR complete | Verify on production | Stretch: notify when a video is ready |

### Handoffs

- **Mon 12:** A posts the base outputs (2.1) → D writes against them all week. C and E agree 2.2; B and E agree 2.4.
- **Tue 13:** E's editor plays the `edit_source` video → C's scrubber and E's crop box mount on it.
- **Tue 13:** A posts the datastore review note → D and Ibrahim's reviewer read it before anything opens.
- **Wed 14:** staging runs the split design → B, C and E verify against it from here on. The crop-pair go / no-go is posted.
- **Thu 15:** D's group is live on staging → A times 4K on a spot worker; A and D run the interruption test together.
- **Wed 14:** B's `DELETE /admin/users/{id}` lands → E wires the button on Thursday.
- **Fri 16:** Ibrahim lowers the DNS TTL at Spaceship, so Saturday's switch takes minutes, not hours.
- **Sat 17, 12:00:** A's cutover decision, which decides where everyone verifies.

### Checkpoints — things you can check, not statuses you post

| By the end of | This is true, or we have a problem |
|---|---|
| Mon 12 | Contracts 2.1, 2.2 and 2.3 posted. ECR holds a worker image built from `main`. |
| Tue 13 | Staging has RDS and ElastiCache, unreachable from outside AWS (tested, not assumed). The review note is posted. |
| Wed 14 | A spot worker on staging takes a job from the queue. `docker stop` mid-encode puts the job back and another worker finishes it. The crop-pair decision is posted. A clip cut with the scrubber comes out the length it showed. |
| Thu 15 | A 4K phone clip plays at 2160p on staging. Production's dump restores cleanly into staging's RDS. |
| Fri 16 | **Freeze:** every track has an open PR. The DNS TTL is 300 s. |
| Sat 17 | flickpond.com on AWS plays a 4K upload in 4K; with the queue empty the worker count falls to the scheduled minimum; a 1 GB upload completes. The Alibaba server is untouched and kept for a week as the rollback. The health alarm is armed. An operator deletes a test account, and its videos are gone from S3. |

---

## 5. Carried over, folded in above

- **Editing from the original + the crop box** — no-go in sprint 4 — C and E.
- **The clip scrubber** — dropped in sprint 4 to keep one new component per
  sprint; the editor has waited for it since sprint 3 ("Coming in the next
  update") — C.
- **Upload follow-ups from #66:** cancel (#67), parallel parts (#68), one
  resume note per upload (#69) — E.
- **Login p90 of 5.8 s** at 50 users — D, Thursday.
- **`docker-compose.yml` still pins the MP4 at 720p** (BUG-15) — the AWS
  overlay sets 1080, so the cutover retires it; D changes the default too.
- **`core_private_ip` as a base output**, asked for by D — A, Monday (2.1).
- **Test accounts left on production** by deploy checks
  (`deploy-check-…@example.com`) — deleted with the new operator option once
  it lands.

## 6. Not this sprint

- **An admin page with real roles and rights** — today there are two roles,
  `user` and `operator`, and the operator can see and delete everything.
  Sprint 6: a proper admin page (users, jobs, storage use), and finer rights
  than all-or-nothing.

- **Chunked parallel encoding** — splitting one video across workers needs
  the worker group live first. Sprint 6.
- **CloudFront** — worth it once video traffic out of S3 costs real money.
- **Workers without database access** (results through the API, the queue on
  SQS) — the managed datastores close most of the exposure for now.
- **Encoding 720p once** — it conflicts with the ladder as its own job, which
  stays; revisit with chunked encoding.

## 7. Rules that have not changed

1. `main` requires a pull request and a review.
2. Ask before editing a file another track owns, and say so in the PR.
3. Every file under `tests/integration/` needs the `RUN_POSTGRES_TESTS` guard.
4. The worker is the sole writer to `status`, `output_key`, `error` and `updated_at` after the API's insert, through `app/repositories/jobs.py` only. Giving a job back on `SIGTERM` (2.3) is a worker write and goes through the same file.
5. Never commit credentials. Check `git diff --cached --name-only` first. On AWS, secrets live in SSM Parameter Store, never in Terraform outputs or `.env` files in the repo.
6. Until the cutover, deploy the current server with `--build --scale worker=2`, then restart `frontend`. After it, the runbook in [`aws-cutover.md`](aws-cutover.md) is the deploy procedure.
7. **New: anything that opens a datastore to the network is reviewed before `apply`**, by someone other than whoever runs it.
