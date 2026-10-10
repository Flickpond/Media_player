# Sprint 4 — Team Record

**Written:** 11 October 2026 · figures from `main` @ `473f117`, the commit
deployed and verified that day
**Sprint window:** Sunday 4 – Sunday 11 October 2026
**Status:** every track's planned work merged and deployed, except the crop
box and editing from the original (a joint no-go, moved to sprint 5) and the
AWS cutover (moved to sprint 5 by decision, not by slip).
**Plan:** [`sprint4-plan.md`](sprint4-plan.md) — 4K, direct uploads, AWS.

---

## 1. Where the project stands

| | |
|---|---|
| Deployed at | **<https://flickpond.com>** — Alibaba ECS `47.238.64.156`. **Decided 8 Oct: production stays here for sprint 4 and moves to AWS in sprint 5**, onto the split core + spot-worker design (see [`roadmap.md`](roadmap.md)) rather than onto one large host first. AWS staging is built and proven, and stays stopped between tests |
| Commit live | `473f117`, deployed 11 Oct after `8a26297` on 10 Oct; both verified end to end on flickpond.com |
| Tests | **733 pass:** 580 Python unit, 153 frontend; the Postgres suite green in CI on `473f117` (653 Python tests collected in all) |
| Merged this sprint | 17 PRs between #49 and #66, from all five tracks |
| 4K end to end | **On AWS staging:** a 20 s 4K clip fully laddered to 2160p in 41 s. **On production:** capped at 1080p by design until the sprint 5 move — the two-core server cannot encode 4K ladders in time |

**What a user gets since this sprint**, verified on flickpond.com on
11 October with a 75 MB upload:

- A thumbnail on every video in the Library.
- Uploads up to **2 GB**, sent straight to storage in 16 MiB parts, with a
  progress bar showing percentage, speed and time left.
- **Resume:** refresh the page mid-upload, pick the same file, and only the
  missing parts are sent.
- The video plays as soon as its MP4 exists; HD qualities arrive moments
  later ("HD versions are still processing").
- An editor that offers only the edits that fit the video, and refuses an
  over-long video before uploading it (5-minute limit).

## 2. Who built what

### Track A — Ibrahim Mammadov (@1brahim74) · processing and AWS

| PR | Day | What | Size |
|---|---|---|---|
| #51 | Sun | Job columns: `width`, `height`, `duration_seconds`, `thumbnail_key`, `hls_status` with a database constraint keeping it in step with `hls_key` | +373 / −9 |
| #52 | Mon | Probe once, first; store the source's size and duration | +177 / −9 |
| #53 | Tue | **The ladder as its own job** on a lower-priority queue: a video is playable as soon as its MP4 is done. The reaper recovers lost or dead ladders | +1098 / −116 |
| #54 | Mon–Tue | Terraform base: S3 (private, CORS for direct upload and hls.js), instance role, security group, Graviton EC2, Elastic IP | +550 / −0 |
| #55 | Sun | Fix for the first apply (BUG-13) | +11 / −5 |
| #56 | Wed | Ladder up to 2160p, never above the source or the host's cap; MP4 up to 1080p; edits get ladders; S3 through the instance role | +207 / −21 |
| #57 | Thu | AWS compose overlay; [cutover runbook](aws-cutover.md) | +283 / −0 |
| #62 | Thu | Roadmap: re-queueing interrupted jobs; the cost of splitting the host | +110 / −15 |
| #63 | Thu | Test fix that unblocked C's branch (BUG-18) | +25 / −7 |

Also on Track A this sprint: two production deploys (10 and 11 Oct), a
4 GB swap file on the production host before the first MinIO build from
source, reviews of every other track's PRs, and the fixes pushed to #60 and
#66 with their owners' agreement (BUG-16, BUG-17).

**Verified end to end on the local stack:**
- A 3840×2160 upload was `done` (playable) with its ladder `pending`, then `ready` 3 s later.
- It stored 3840×2160 and got ladder rungs 360–1080 under the default cap.
- A 720p edit of it stored 1280×720 and got a ladder topped at 720.

**4K on AWS staging** (4 Oct; `c7g.2xlarge`, 8 vCPU / 16 GB; two workers):

| Stage | Time |
|---|---|
| Upload → playable (1080p MP4 `done`) | 11 s |
| Ladder, six rungs 360p → 2160p | 28 s |
| Upload → 4K ready | 41 s |

The source was a synthetic 20 s 3840×2160 clip at 35 Mbps (89 MB) with
per-frame grain, so that every frame costs the encoder something, as real
footage does. The ladder ran at about 1.4× real time, so the 1770 s timeout
covers about 20 minutes of 4K. Today the 100 MB upload cap binds first, at
about 20 s of phone 4K; B's direct upload is what lifts that.

Still to confirm with a real phone clip (sprint 5, Thursday), since a
synthetic source is only an approximation of camera footage.

### Track B — Zhang Jizhang (@zhanj384) · upload path

| PR | What | Size |
|---|---|---|
| #60 | **Direct, resumable multipart upload:** sessions, per-part presigned PUTs, resume from what storage holds, retry-safe completion, the content sniff by range read, octet-stream + attachment until validated, abort, expired-session cleanup in the reaper, a 2 GiB cap | +1519 / −4 |

### Track C — Yang Dongwei (@ttydw-ch) · media rules and thumbnails

| PR | What | Size |
|---|---|---|
| #65 | **Probe-first rejection** (too long, corrupt, no video stream fails in seconds); **thumbnails** taken right after the probe; **source-aware scale rules** and `edit_options`, so `/edit` refuses an impossible scale at once; `GET /limits`; upright phone video recognised as portrait | +1824 / −26 |

**Editing from the original: no-go**, posted on Wednesday together with E.
Moves to sprint 5 with the crop box, under the same pairing rule.

### Track D — Jiang Yibai (@JiangYibai666) · infrastructure

| PR | What | Size |
|---|---|---|
| #49 | Load-test framework and report (last week's work, merged on day 1): reads meet the 2 s target at 50 users; login p90 5.8 s | +785 / −0 |
| #59 | **MinIO built from pinned source**, so a fresh clone starts the stack | +34 / −24 |
| #61 | **Sprint 5 groundwork:** SonarCloud and DAST able to fail a merge (with integration coverage reaching Sonar); queue-depth metric publisher for CloudWatch; the ECR registry and CI image pipeline; the spot worker autoscaling module, `plan` only; trap T-25; the login-latency investigation | +2650 / −52 |

### Track E — Lu Jingxing (@Dilute-l) · the UI

| PR | What | Size |
|---|---|---|
| #64 | Library poster frames | +99 / −1 |
| #66 | **The upload page on direct upload:** progress (percent, rate, time left), resume after a refresh, the editor offering only heights that fit the video, "HD processing" from `hls_status`, the duration limit in minutes and refused before upload | +1511 / −98 |

**Crop box: no-go**, with C — sprint 5.

## 3. Bug log

Continues from BUG-12 in [`sprint2-report.md`](sprint2-report.md).

### BUG-13 — The first `terraform apply` left staging without internet access · **major** · fixed #55

Both outbound security-group rules failed with HTTP 400. Their description
contained an apostrophe ("Let's Encrypt"), which AWS rejects in rule
descriptions. `terraform validate` and `plan` both passed, so the error only
appeared at apply time. The other 17 resources were created. The instance
booted with no outbound access, so its one-time install of Docker and git
failed silently. **Fix:** remove the apostrophe; rebuild the instance once
with `-replace` (runbook step 1).

### BUG-14 — SonarCloud failed #51 on code that was tested · **minor** · fixed #52

The new-code coverage gate reported 66.7%. The repository logic that #51
added was only exercised by the Postgres suite, and CI runs without
`RUN_POSTGRES_TESTS`, so to SonarCloud the code looked untested. **Fix:**
unit tests for the logic itself; every Track A PR since has been checked for
coverage of its changed lines under unit tests alone before pushing. The
lasting fix is for CI to run the Postgres suite — see §5.

### BUG-15 — The MP4 stays at 720p on any host using the default compose file · **minor** · open

The application default rose to 1080 (#56), but `docker-compose.yml` passes
`${WORKER_FFMPEG_MAX_HEIGHT:-720}`, which overrides it. Found during #56's
end-to-end check. The AWS overlay sets 1080. On the current server, set
`WORKER_FFMPEG_MAX_HEIGHT=1080` in `.env`. The compose default is Track D's
file and needs D's agreement to change.

### BUG-16 — Direct upload had no size limit · **critical** · fixed in #60 before merge

`POST /uploads` accepted up to 16 MiB × 10,000 parts — about 160 GB — with
any number of sessions per user. Production's MinIO and Postgres share a
disk with 12 GB free at the time, so one large upload could fill it and stop
the database. Found in review. **Fix:** `MAX_UPLOAD_BYTES`, 2 GiB, refused
with 413 before a session opens.

### BUG-17 — One dropped part ended a whole upload; the size error was a byte count · **moderate** · fixed in #66 before merge

Parts go one at a time and a failed PUT was never retried, so a network blip
on part 40 of 64 stopped the upload (resumable, but only after picking the
file again). The 2 GiB refusal reached the page as "exceeds 2147483648
bytes". Found in review and confirmed in a browser. **Fix:** up to three
tries per part for network errors, 5xx, 408 and 429 (a 403 is not retried);
`max_upload_bytes` in `GET /limits`, so the page says "up to 2 GB" and
refuses a larger file before sending any of it.

### BUG-18 — Two edit tests assumed edits never probe their source · **minor** · fixed #63

Track A's tests from #56 answered every probe with one size. C's
source-aware rules rightly probe the source too, so merging C's branch
failed both — a fixture fault, not C's. **Fix:** the fake prober answers per
file, as ffprobe would.

## 4. Known limitations

- **Terraform state is on one laptop.** It moves to an S3 backend before
  D's autoscaling module is applied (sprint 5).
- **A video deleted while its ladder is building** is handled. The ladder
  job removes its own segments, because the delete route can't find them
  without an `hls_key`.
- **A 4K ladder is one FFmpeg run.** A long 4K upload is bounded by the
  1770 s timeout on AWS. Chunked parallel encoding is on the roadmap for
  sprint 6.
- **Production's disk:** 7.4 GB free on 11 October, shared by MinIO and
  Postgres, while uploads may now be 2 GB. Enough for a week of normal use;
  the AWS move in sprint 5 removes the constraint.
- **No way to delete an account.** Test accounts made by the deploy checks
  remain on production. Operator user deletion is in sprint 5.

## 5. What is left

All of it is in [`sprint5-plan.md`](sprint5-plan.md):

- **The move to AWS**, onto a small core, RDS, ElastiCache and spot workers.
- **Editing from the original + the crop box** (no-go this sprint), and the
  clip scrubber the editor has waited for since sprint 3.
- **Upload follow-ups** from #66: cancel (#67), parallel parts (#68), one
  resume note per upload (#69).
- **Login p90 of 5.8 s** at 50 users.
- **BUG-15**, the compose file's 720p default.
