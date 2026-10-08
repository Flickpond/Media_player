# Sprint 4 — Team Record

> **Draft.** Track A's section is complete and its figures are checkable
> today. Everything marked **TODO** is filled in on Sunday 11 October, once
> the other tracks have merged and Saturday's cutover decision is made.

**Sprint window:** Sunday 4 – Sunday 11 October 2026
**Plan:** [`sprint4-plan.md`](sprint4-plan.md) — 4K, direct uploads, AWS.

---

## 1. Where the project stands

| | |
|---|---|
| Deployed at | **<https://flickpond.com>** — Alibaba ECS `47.238.64.156`. **Decided 8 Oct: production stays here for sprint 4 and moves to AWS in sprint 5**, onto the split core + spot-worker design (see [`roadmap.md`](roadmap.md)) rather than onto one large host first. AWS staging is built and proven, and stays stopped between tests |
| Commit live | **TODO** |
| Tests | **TODO** at freeze. Track A's last branch: 419 Python unit tests pass, 59 Postgres integration tests pass |
| 4K end to end on production | **TODO** — the Saturday checkpoint |

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
| #57 | Thu | AWS compose overlay; [cutover runbook](aws-cutover.md), including the password rotation | +283 / −0 |

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

**TODO (Track A):**
- The same run with a real phone clip, to confirm the synthetic figure.
- Whether the admin password was rotated, and on which server.

### Tracks B, C, D, E

**TODO** — one table per track, in the same form.

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

**TODO** — bugs from the other tracks.

## 4. Known limitations

- **Terraform state is on one laptop.** It moves to an S3 backend before
  D's autoscaling module is applied (sprint 5).
- **A video deleted while its ladder is building** is handled. The ladder
  job removes its own segments, because the delete route can't find them
  without an `hls_key`.
- **A 4K ladder is one FFmpeg run.** A long 4K upload is bounded by the
  1770 s timeout on AWS. Chunked parallel encoding is on the roadmap for
  sprint 5 and later.

## 5. What is left

- CI runs the Postgres suite, so SonarCloud sees the integration tests (D, if they agree).
- Remote Terraform state; D's autoscaling module applied (sprint 5).
- **TODO** — the other tracks' carry-overs.
