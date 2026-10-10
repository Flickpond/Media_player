# Sprint 5 — Plan

**Window:** Monday 12 October – Sunday 18 October 2026
**Freeze:** Friday 16 · **Cutover and verify:** Saturday 17 · **Report:** Sunday 18
**Written against:** `main` @ `473f117` · from [`roadmap.md`](roadmap.md) and
[`sprint4-report.md`](sprint4-report.md)

**Sprint goal:** flickpond.com runs on AWS — a small always-on core, managed
Postgres and Redis, and spot workers that scale with the queue — and plays a
phone's 4K video in 4K. Edits start from the original upload, with a crop box
measured in its pixels.

One member, one function, as in sprints 3 and 4. Weekends count as working
days. **Read [`known-traps.md`](known-traps.md) before writing code.**

---

## 0. Who has what

| Track | Owner | Function | Main deliverable |
|---|---|---|---|
| **A** | Ibrahim Mammadov (@1brahim74) | Processing and AWS | RDS and ElastiCache in the base; workers that survive being stopped; the cutover |
| **B** | Zhang Jizhang (@zhanj384) | Upload path | Direct upload proven on S3; one upload path instead of two |
| **C** | Yang Dongwei (@ttydw-ch) | Media rules | Editing from the original upload |
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

### 2.2 Cropping the original (C builds it, E draws it)

```
POST /jobs/{id}/edit
  { "operations": [ { "operation": "crop", "params": { "x", "y", "w", "h" } }, ... ] }
```

- An edit **always starts from the original upload**, even when `{id}` is
  itself an edit: the API follows the chain to the root job and uses its
  `source_key`. Edits of edits no longer lose quality at each step.
- Crop coordinates are in **the original's pixels** — the `width` and
  `height` on the root job's row. The page scales the box from what it shows
  on screen to those numbers before sending.
- `GET /jobs/{id}` gains `edit_source: { "job_id", "width", "height" }`, so the
  page knows which video it is really cropping without walking the chain itself.
- A crop outside the original's frame stays a **worker** failure with a
  readable error (sprint 3's rule); the API checks only the shape of the request.

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

---

## 3. The one pairing rule, again

**Editing from the original (C) and the crop box (E) ship together, or
neither ships** — the same rule as sprint 4, where both were a no-go. The
crop rectangle only means something in the original's pixels once edits
start from the original. **Decide on Wednesday 14.**

---

## 4. Schedule

**How each day ends:** open or update your PR by end of day; Ibrahim reviews
and merges that evening. Small daily PRs, not one branch that lands on Friday.

| | Mon 12 | Tue 13 | Wed 14 | Thu 15 | **Fri 16 · freeze** | **Sat 17 · cutover** | Sun 18 |
|---|---|---|---|---|---|---|---|
| **A** Ibrahim | Terraform: RDS, ElastiCache, their security groups, SSM secrets, the outputs in 2.1; remote state with locking. Post contracts 2.1 and 2.3. | Worker `SIGTERM` handling: give the job back, attempt cap of 3, clear partial output. **Post the datastore review note.** | Apply the datastores on staging once the note is read; core shrinks to `t4g.small`. RQ heartbeats; shorter reaper lease. Rewrite CLAUDE.md rule 2. | Time a real 4K phone clip through a spot worker. Rehearse the data move: restore a copy of production's dump into staging's RDS. Update the cutover runbook for RDS. | PR complete | **12:00 decision.** Cutover: stop writes, `pg_dump` → RDS, MinIO → S3, certificates, DNS. Verify a 4K upload plays in 4K. | Sprint report |
| **B** Zhang | Cap open upload sessions per user. Plan retiring `POST /upload` with E: what the fallback still covers. | Direct upload on staging S3: CORS from the staging origin, `ETag` exposed, a resumed upload. | Files the browser can't type go through the direct path too (the content sniff on `complete` already identifies them); `POST /upload` then retires. | Tests; a 1 GB upload on staging, resumed once. | PR complete | Verify a 1 GB upload on production | Slack |
| **C** Yang | Agree contract 2.2 with E. Design: finding the root job; what `edit_source` returns for uploads, edits and edits of edits. | Edit from the original: the API follows the chain; `edit_source` in the API. | Tests across the edit path, including an edit of an edit. **Go / no-go** with E. | If go: verify crop and scale on staging against a 4K original. Raise the duration limit if A's 4K timing allows it. | PR complete | Verify on production | Slack |
| **D** Yibai | Apply `infra/registry`; set the repository variables so CI pushes a worker image per merge. Repoint the module's datastore rules at 2.1's groups. | `terraform plan` the worker group against staging with the datastores open; read A's review note. | Apply the worker group on staging: spot, queue-depth scaling, scheduled minimum. Spot interruption → `docker stop` (2.3). | Interruption test with A (AWS FIS, or `docker stop` mid-encode). The 5.8 s login fix if the investigation found one; DAST against staging. | PR complete | Cutover with A: workers live on production; watch the scaling alarms. | Write-up: the first week's real AWS cost |
| **E** Lu | Agree contract 2.2 with C. Cancel an upload in progress (#67). | Crop box drawn over the original, converted to its pixels via `edit_source`. | Upload parts three at a time, measured before and after (#68). **Go / no-go** with C. | One resume note per upload (#69). Quality labels up to 2160p; a "starting a worker…" note when a job waits on a cold start. | PR complete | Verify on production | Stretch: notify when a video is ready |

### Handoffs

- **Mon 12:** A posts the base outputs (2.1) → D writes against them all week. C and E agree 2.2.
- **Tue 13:** A posts the datastore review note → D and Ibrahim's reviewer read it before anything opens.
- **Wed 14:** staging runs the split design → B, C and E verify against it from here on. The crop-pair go / no-go is posted.
- **Thu 15:** D's group is live on staging → A times 4K on a spot worker; A and D run the interruption test together.
- **Sat 17, 12:00:** A's cutover decision, which decides where everyone verifies.

### Checkpoints — things you can check, not statuses you post

| By the end of | This is true, or we have a problem |
|---|---|
| Mon 12 | Contracts 2.1, 2.2 and 2.3 posted. ECR holds a worker image built from `main`. |
| Tue 13 | Staging has RDS and ElastiCache, unreachable from outside AWS (tested, not assumed). The review note is posted. |
| Wed 14 | A spot worker on staging takes a job from the queue. `docker stop` mid-encode puts the job back and another worker finishes it. The crop-pair decision is posted. |
| Thu 15 | A 4K phone clip plays at 2160p on staging. Production's dump restores cleanly into staging's RDS. |
| Fri 16 | **Freeze:** every track has an open PR. |
| Sat 17 | flickpond.com on AWS plays a 4K upload in 4K; with the queue empty the worker count falls to the scheduled minimum; a 1 GB upload completes. The Alibaba server is untouched and kept for a week as the rollback. |

---

## 5. Carried over, folded in above

- **Editing from the original + the crop box** — no-go in sprint 4 — C and E.
- **Upload follow-ups from #66:** cancel (#67), parallel parts (#68), one
  resume note per upload (#69) — E.
- **Login p90 of 5.8 s** at 50 users — D, Thursday.
- **`docker-compose.yml` still pins the MP4 at 720p** (BUG-15) — the AWS
  overlay sets 1080, so the cutover retires it; D changes the default too.
- **`core_private_ip` as a base output**, asked for by D — A, Monday (2.1).
- **The admin password** has been in chat logs since 11 September. If it
  wasn't rotated by the end of sprint 4, it is rotated on the new production
  host during the cutover — never pasted into chat again.

## 6. Not this sprint

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
