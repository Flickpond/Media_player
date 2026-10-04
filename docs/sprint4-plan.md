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
| **A** | Ibrahim Mammadov (@1brahim74) | Processing | Thumbnails, the HLS ladder as its own job, ladder up to 2160p |
| **B** | Zhang Jizhang (@zhanj384) | Upload path | Direct, resumable multipart upload to storage |
| **C** | Yang Dongwei (@ttydw-ch) | Edit correctness | Source-aware edit rules, duration limits, edit from the original |
| **D** | Jiang Yibai (@JiangYibai666) | Infrastructure | AWS staging via Terraform, S3, security gates that can fail |
| **E** | Lu Jingxing (@Dilute-l) | The UI | Upload progress bar, thumbnails, options that fit the video, crop box |

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

---

## 2. The contracts — agree these on Sunday and Monday

### 2.1 What a job tells the UI (A builds it, E and C read it)

```
GET /jobs/{id}
  { ...existing fields...,
    "width": 3840, "height": 2160, "duration_seconds": 41.2,   // once probed
    "thumbnail_url": "https://...",                            // once extracted
    "hls_status": "pending" | "ready" | "unavailable" }
```

`hls_status` is what lets the page say *"HD versions are still processing"*
instead of guessing from a missing `hls_url`. `unavailable` covers jobs from
before sprint 3 and ladders that failed.

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
  `localhost:3000` and MinIO on `127.0.0.1:9000` are different origins.
- **Content sniffing must survive.** Today `POST /upload` reads the first 4 KB
  to confirm the file really is a video — that's the sprint 1 stored-XSS fix.
  With direct upload the API never sees those bytes, so `complete` reads them
  back from storage with a range request and rejects anything that isn't a
  recognised video container before creating the job.
- **Use 16 MiB parts.** Above S3's 5 MiB minimum, and below the 101 MB nginx
  body limit, which still applies on the current server where storage is
  reached through nginx's `/videos/` proxy.

The old `POST /upload` keeps working until the UI has switched over.

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
| **A** Ibrahim | Migration: `width`, `height`, `duration_seconds`, `thumbnail_key`, `hls_status`. Post contract 2.1. | Probe and store dimensions at the start of processing; extract a thumbnail frame. Rotate the admin password. | Ladder as its own job: queued once the MP4 exists; `hls_status` pending → ready / unavailable. | Ladder up to 2160p, never above the source; rung cap as a setting; MP4 fallback ≤ 1080p; edited videos get a ladder too. | Tests; time a real 4K clip on staging with D. | PR complete | Deploy; verify a 4K upload plays in 4K | Sprint report |
| **B** Zhang Jizhang | Read contract 2.2; agree it with E. | `POST /uploads` and part signing, against local MinIO. | `complete`: content sniff via range read, then create the job and enqueue. | Resume (`GET /uploads/{id}`), abort, local MinIO CORS. | Tests; help E switch over. | PR complete | Verify a 1 GB upload end to end | Slack |
| **C** Yang Dongwei | Agree the scale rules with A and E. | Duration limit and probe-first rejection: too long, corrupt, no video stream — fails in seconds. | Source-aware scale rules; `/edit` returns 422 immediately using stored dimensions. | **Go / no-go** on edit-from-original with E. If go: implement. | Tests across the edit path. | PR complete | Verify on production | Slack |
| **D** Jiang Yibai | Confirm the AWS account. Get #49 merged. Terraform skeleton. | Terraform: S3 (with CORS exposing `ETag`), IAM role, security group, EC2, Elastic IP → staging. | Staging runs the stack on S3. Local Compose builds MinIO from source, so a fresh clone starts. | SAST and DAST able to fail a merge. | Investigate login p90 (5.8 s at 50 users) on staging. Add trap T-25. | PR complete; cutover steps written | **12:00 cutover decision**: AWS if staging passed, else the current server | Write-up |
| **E** Lu Jingxing | Agree contracts 2.1 and 2.2. | Thumbnails in the Library, against a stub until A lands. | Upload progress bar on the multipart API: percent, speed, time left. | Resume after a refresh or dropped connection. **Go / no-go** on the crop box with C. | Options from the video's real height (hide Upscale at 2160p); "HD processing" from `hls_status`; limits in minutes; crop box if go. | PR complete | Verify on production | Stretch: notify when a video is ready |

### Handoffs

- **Sun 4 → Mon 5:** A's migration and contract 2.1 unblock C and E.
- **Mon 5:** contract 2.2 agreed — B and E build against it in parallel.
- **Tue 6:** B's `complete` works → E stops using the stub.
- **Wed 7:** the crop-pair go / no-go is posted in the group.
- **Thu 8:** D's staging is up → A times a real 4K clip on it.
- **Sat 10, 12:00:** D's cutover decision, which decides where everyone verifies.

### Checkpoints — things you can check, not statuses you post

| By the end of | This is true, or we have a problem |
|---|---|
| Mon 5 | Migration merged; both contracts posted; #49 merged; the AWS account confirmed or AWS moved to sprint 5. |
| Tue 6 | A local upload shows a thumbnail. A multipart upload completes against local MinIO using only `curl`. |
| Wed 7 | The ladder runs as its own job. The crop-pair decision is posted. |
| Thu 8 | `terraform apply` from nothing builds staging, and one upload plays there. |
| Fri 9 | **Freeze:** every track has an open PR. |
| Sat 10 | Production plays a 4K upload in 4K, shows thumbnails, and completes a 1 GB upload with a progress bar. |

---

## 5. Carried from sprint 3, folded in above

- Load-test numbers — **#49**, merge on day 1.
- Login p90 of 5.8 s at 50 users — D, Thursday.
- Security gates that can't fail — D, Wednesday.
- A fresh clone can't pull MinIO — D, Tuesday.
- Trap T-25: restart nginx after a deploy that recreates the API — D, Thursday.
- The admin password has been in chat logs since 11 September — A, Monday.

## 6. Not this sprint

- **Encoding 720p once** — conflicts with the ladder as its own job (§1). Sprint 5.
- **Clip scrubber UI** — the crop box is this sprint's one new interactive component.
- **Worker Auto Scaling Group, chunked encoding, CloudFront** — sprint 5 and later, per the roadmap.

## 7. Rules that have not changed

1. `main` requires a pull request and a review.
2. Ask before editing a file another track owns, and say so in the PR.
3. Every file under `tests/integration/` needs the `RUN_POSTGRES_TESTS` guard.
4. The worker is the sole writer to `status`, `output_key`, `error` and `updated_at` after the API's insert, through `app/repositories/jobs.py` only.
5. Never commit credentials. Check `git diff --cached --name-only` first.
6. Deploy with `--build --scale worker=2`, then restart `frontend`.
