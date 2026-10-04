# Sprint 4 — Plan

**Window:** Sunday 4 October – Sunday 11 October 2026
**Freeze:** Friday 9 · **Deploy and verify:** Saturday 10 · **Report:** Sunday 11
**Written against:** `main` @ `5566758` · from [`roadmap.md`](roadmap.md)

**Sprint goal:** a phone's 4K video uploads with a progress bar, plays in 4K
when the connection allows, and the editor offers only the edits that make
sense for it.

One member, one function — the same shape as sprint 3. Weekends count as
working days. **Read [`known-traps.md`](known-traps.md) before writing code.**

---

## 0. Who has what

| Track | Owner | Function | Main deliverable |
|---|---|---|---|
| **A** | Ibrahim Mammadov (@1brahim74) | Processing and AWS | The HLS ladder as its own job and up to 2160p; AWS staging via Terraform; the cutover |
| **B** | Zhang Jizhang (@zhanj384) | Upload path | Direct, resumable multipart upload to storage |
| **C** | Yang Dongwei (@ttydw-ch) | Media rules and thumbnails | Thumbnails, duration limits, source-aware edit rules, editing from the original |
| **D** | Jiang Yibai (@JiangYibai666) | Infrastructure | Security gates that can fail; groundwork for sprint 5's autoscaling workers |
| **E** | Lu Jingxing (@Dilute-l) | The UI | Upload progress bar, thumbnails, options that fit the video, crop box |

**Track A is the heaviest track.** If it slips, the AWS cutover is what waits:
the fallback is the current server, which already works.

---

## 1. Decisions already made

- **The ladder becomes its own job; encoding 720p once waits.** The roadmap
  listed both, but they conflict: once the MP4 and the ladder run in separate
  jobs, they can't share one 720p encode. The separate job wins this sprint —
  it lets people watch sooner and gives each half its own timeout. The shared
  encode moves to sprint 5.
- **The MP4 fallback stays at most 1080p.** The ladder goes up to 2160p, but
  the single-file MP4 is for compatibility and download, and doesn't need 4K.
- **Everything is built against the S3 API.** MinIO speaks the same API, so
  every track works locally with MinIO and nothing waits for AWS. Moving to
  S3 is a configuration change.
- **If there's no AWS account with credits by Monday**, the AWS move becomes
  sprint 5 and everything else still ships, on the current server. On that
  server, set the ladder cap to 1080p — two CPUs can't encode 4K ladders
  within the timeouts.
- **The worker autoscaling group is prepared this sprint and applied next.**
  Applying it needs A's AWS base live and Postgres/Redis opened to the private
  network, which is too much to land in the same week as the move itself.

---

## 2. The contracts — agree these on Sunday and Monday

### 2.1 What a job tells the UI (A and C build it, E reads it)

```
GET /jobs/{id}
  { ...existing fields...,
    "width": 3840, "height": 2160, "duration_seconds": 41.2,   // once probed (A)
    "thumbnail_url": "https://...",                            // once extracted (C)
    "hls_status": "pending" | "ready" | "unavailable" }        // (A)
```

`hls_status` is what lets the page say *"HD versions are still processing"*
instead of guessing from a missing `hls_url`. `unavailable` covers jobs from
before sprint 3 and ladders that failed. A writes the single migration for
all of these columns, including C's `thumbnail_key`.

### 2.2 Direct upload (B builds it, E calls it)

```
POST   /uploads                      { filename, size, content_type }
  -> 201 { upload_id, part_size, part_count }
POST   /uploads/{upload_id}/parts    { part_numbers: [1, 2, 3] }
  -> 200 { urls: { "1": "https://...", "2": "...", ... } }   short-lived presigned PUTs
GET    /uploads/{upload_id}          -> { parts_done: [ { n, etag } ] }   for resuming
POST   /uploads/{upload_id}/complete { parts: [ { n, etag } ] }
  -> 202 { job_id }                  the job row and the queue entry are created only now
DELETE /uploads/{upload_id}          abort, and free the stored parts
```

The browser sends each part **directly to storage**; the API never touches
the video bytes. Three things that will break this if missed:

- **The browser must be able to read each part's `ETag` response header.**
  The bucket's CORS rules must allow `PUT` from the site's origin *and* list
  `ETag` under exposed headers. Without that, every upload uploads fine and
  then fails at `complete`. Locally this applies too: the page on
  `localhost:3000` and MinIO on `127.0.0.1:9000` are different origins. On
  AWS, A sets this in Terraform.
- **Content sniffing must survive.** Today `POST /upload` reads the first 4 KB
  to confirm the file really is a video — that's the sprint 1 stored-XSS fix.
  With direct upload the API never sees those bytes, so `complete` reads them
  back from storage with a range request and rejects anything that isn't a
  recognised video container before creating the job.
- **Use 16 MiB parts.** Above S3's 5 MiB minimum, and below the 101 MB nginx
  body limit, which still applies on the current server where storage is
  reached through nginx's `/videos/` proxy.

The old `POST /upload` keeps working until the UI has switched over.

### 2.3 The Terraform split (A and D agree on Sunday)

A owns the **base**: S3, IAM, the security group, the EC2 instance, the
Elastic IP. D's autoscaling module **plugs into it** and must not edit it —
agree on Sunday which outputs the base exposes (VPC and subnet IDs, the
security group ID, the bucket name) so D can write against them all week.

---

## 3. The one pairing rule

**Editing from the original upload (C) and the crop box (E) ship together, or
neither ships.** Once edits start from the original, the crop rectangle has to
be measured in the original's pixels, not the 720p player's. **Decide on
Wednesday:** if the crop box won't be ready by Friday, both move to sprint 5
and edits keep using the current source.

---

## 4. Schedule

**How each day ends:** open or update your PR by end of day; Ibrahim reviews
and merges that evening. Small daily PRs, not one branch that lands on Friday.

| | Sun 4 | Mon 5 | Tue 6 | Wed 7 | Thu 8 | **Fri 9 · freeze** | **Sat 10 · deploy** | Sun 11 |
|---|---|---|---|---|---|---|---|---|
| **A** Ibrahim | Migration: dimensions, `thumbnail_key`, `hls_status`. Post contract 2.1. Confirm the AWS account; agree the Terraform split with D. | Probe and store dimensions. Terraform: S3 with CORS exposing `ETag`, IAM role. | Ladder as its own job (`hls_status` pending → ready / unavailable). Terraform: security group, EC2, Elastic IP → staging up. | Ladder up to 2160p, never above the source; rung cap as a setting; MP4 ≤ 1080p; edits get a ladder. Staging runs on S3. | Time a real 4K clip on staging. Write the cutover steps. Rotate the admin password. | PR complete | **12:00 cutover decision**: AWS if staging passed, else the current server. Deploy; verify a 4K upload plays in 4K. | Sprint report |
| **B** Zhang Jizhang | Read contract 2.2; agree it with E. | `POST /uploads` and part signing, against local MinIO. | `complete`: content sniff via range read, then create the job and enqueue. | Resume (`GET /uploads/{id}`), abort, local MinIO CORS. | Tests; help E switch over. | PR complete | Verify a 1 GB upload end to end | Slack |
| **C** Yang Dongwei | Agree the scale rules with A and E. | Duration limit and probe-first rejection: too long, corrupt, no video stream — fails in seconds. | Thumbnail: one frame right after the probe; `thumbnail_url` in the API. | Source-aware scale rules; `/edit` returns 422 at once using stored dimensions. **Go / no-go** on edit-from-original with E. | If go: edit from the original. Tests across the edit path. | PR complete | Verify on production | Slack |
| **D** Jiang Yibai | Get #49 merged. Agree the Terraform split with A. | Local Compose builds MinIO from source, so a fresh clone starts. | SAST and DAST able to fail a merge. Add trap T-25. | *Sprint 5 groundwork:* queue-length metric publisher (RQ → CloudWatch, tested with a stub); CI builds and pushes the worker image to ECR. | *Sprint 5 groundwork:* autoscaling module — spot, scaled by queue length, scheduled minimum. `terraform plan` only. Investigate the 5.8 s login on staging. | PR complete | Verify on production; login findings written up | Write-up: what sprint 5 needs to apply the group |
| **E** Lu Jingxing | Agree contracts 2.1 and 2.2. | Thumbnails in the Library, against a stub until C lands. | Upload progress bar on the multipart API: percent, speed, time left. | Resume after a refresh or dropped connection. **Go / no-go** on the crop box with C. | Options from the video's real height (hide Upscale at 2160p); "HD processing" from `hls_status`; limits in minutes; crop box if go. | PR complete | Verify on production | Stretch: notify when a video is ready |

### Handoffs

- **Sun 4:** A and D agree the Terraform split, so D's module plugs into A's base.
- **Sun 4 → Mon 5:** A's migration and contract 2.1 unblock C and E.
- **Mon 5:** contract 2.2 agreed — B and E build against it in parallel.
- **Tue 6:** B's `complete` works → E stops using the stub. C's thumbnails land → E shows real ones.
- **Wed 7:** the crop-pair go / no-go is posted in the group.
- **Thu 8:** A's staging is up → A times a real 4K clip, D runs the login investigation and `terraform plan`s the autoscaling module against it.
- **Sat 10, 12:00:** A's cutover decision, which decides where everyone verifies.

### Checkpoints — things you can check, not statuses you post

| By the end of | This is true, or we have a problem |
|---|---|
| Mon 5 | Migration merged; all three contracts posted; #49 merged; the AWS account confirmed or AWS moved to sprint 5. |
| Tue 6 | A local upload shows a thumbnail. A multipart upload completes against local MinIO using only `curl`. A fresh clone starts the stack. |
| Wed 7 | The ladder runs as its own job. The crop-pair decision is posted. A failing security finding blocks a test PR. |
| Thu 8 | `terraform apply` from nothing builds staging, and one upload plays there. `terraform plan` for the autoscaling module runs clean against it. |
| Fri 9 | **Freeze:** every track has an open PR. |
| Sat 10 | Production plays a 4K upload in 4K, shows thumbnails, and completes a 1 GB upload with a progress bar. |

---

## 5. Carried over, folded in above

- Load-test numbers — **#49** (last week), merge on day 1.
- Login p90 of 5.8 s at 50 users, found by that load test — D, Thursday.
- Security gates that can't fail (sprint 3) — D, Tuesday.
- A fresh clone can't pull MinIO (sprint 3) — D, Monday.
- Trap T-25: restart nginx after a deploy that recreates the API — D, Tuesday.
- The admin password has been in chat logs since 11 September — A, Thursday.

## 6. Not this sprint

- **Encoding 720p once** — conflicts with the ladder as its own job (§1). Sprint 5.
- **Applying the worker autoscaling group** — D prepares it this sprint; it goes live in sprint 5, together with opening Postgres and Redis to the private network.
- **Clip scrubber UI** — the crop box is this sprint's one new interactive component.
- **Chunked encoding, CloudFront** — sprint 5 and later, per the roadmap.

## 7. Rules that have not changed

1. `main` requires a pull request and a review.
2. Ask before editing a file another track owns, and say so in the PR.
3. Every file under `tests/integration/` needs the `RUN_POSTGRES_TESTS` guard.
4. The worker is the sole writer to `status`, `output_key`, `error` and `updated_at` after the API's insert, through `app/repositories/jobs.py` only.
5. Never commit credentials. Check `git diff --cached --name-only` first.
6. Deploy with `--build --scale worker=2`, then restart `frontend`.
