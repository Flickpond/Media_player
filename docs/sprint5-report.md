# Sprint 5 — Team Record

**Written:** 19 October 2026 · figures from `main` @ `77c6185` (#89), the last
sprint 5 change; staging measurements from 18–19 October
**Sprint window:** Monday 12 – Sunday 18 October 2026
**Status:** every track's planned work merged, and the whole AWS design
built and proven on staging — managed datastores, CloudFront, a container
registry fed by CI, and spot workers scaling from zero. **The production
cutover did not run** and moves to sprint 6 (Tuesday 20 October).
**Plan:** [`sprint5-plan.md`](sprint5-plan.md) — AWS on the split design,
edits from the original.

---

## 1. Where the project stands

| | |
|---|---|
| Deployed at | **<https://flickpond.com>** — still Alibaba ECS `47.238.64.156`, at `735da33` (#80, deployed during the sprint). **AWS staging** — <https://47-128-86-77.sslip.io> — runs `main` with everything below |
| Tests | **1,012 pass at `77c6185`:** 766 Python with the Postgres suite (694 without it), 246 frontend |
| Merged this sprint | 20 PRs, #70 – #89, from all five tracks. #70 – #80 merged on 10 October, ahead of the window |
| 4K end to end | **On staging, a real 4K HDR phone clip** (384 MB, 2 min 7 s, AV1 10-bit): MP4 ready in **5 min 25 s**, the full ladder 360p – 2160p **13 min 26 s** later, on one spot worker |
| AWS cost | Not yet measured over a full week — D's write-up after the cutover |

**What a user gets since this sprint** (verified on staging, 18–19 Oct):

- **Edits start from the original upload.** An edit of an edit no longer
  re-encodes a re-encoded copy; the editor says "X — the original of Y".
- **A crop box** drawn on a still of the paused frame, in the original's
  pixels, with aspect presets, and **a clip scrubber** with two handles and
  a readout. The editor no longer says "Coming in the next update".
- **HDR phone videos play everywhere**, tone-mapped to standard range,
  instead of a 10-bit file no browser plays.
- **Keep 60 fps**, labelled "720p60" in the quality menu.
- **Uploads:** cancel mid-upload, three parts at a time, and one resume note
  per upload.
- **Operators can delete an account** with everything it stored, from an
  Accounts table on the admin page.
- A job waiting for a worker says "starting a worker, this can take a few
  minutes" instead of an unexplained "Queued...".

## 2. Who built what

### Track A — Ibrahim Mammadov (@1brahim74) · processing and AWS

| PR | Merged | What | Size |
|---|---|---|---|
| #70 | 10 Oct | The sprint 5 plan | +260 / −0 |
| #71 | 10 Oct | 4K for less: the ladder cascades from 1080p, capped CRF per rung; **Keep 60 fps** as the uploader's choice | +626 / −59 |
| #72 | 10 Oct | RDS PostgreSQL and ElastiCache Valkey (private, TLS-only, auth token), secrets in SSM, Terraform state in S3 with locking | +554 / −36 |
| #73 | 10 Oct | **A stopped worker gives its job back** instead of failing it: SIGTERM stops FFmpeg, the job returns to `queued`, at most 3 times | +621 / −24 |
| #74 | 10 Oct | The reaper finds a dead worker by its missing RQ heartbeat (5 min), not after the whole 40-minute lease | +212 / −11 |
| #75 | 10 Oct | The AWS core runs against RDS and ElastiCache; `fetch-secrets.sh` builds `.env` from SSM | +386 / −137 |
| #76, #77 | 10 Oct | Datastore apply fixes: RDS `force_ssl` drift; asyncpg's `ssl=require`; security-group-to-group egress from the core | +25 / −5 |
| #78 | 10 Oct | Original uploads move to S3 Standard-IA after 30 days | +20 / −0 |
| #79 | 10 Oct | Each ladder rung encoded to a quality, capped at its bitrate | +46 / −5 |
| #80 | 10 Oct | **HLS segments through CloudFront** with signed URLs; MP4 and thumbnails stay on S3 | +451 / −4 |
| #85 | 18 Oct | **HDR uploads tone-mapped** to 8-bit BT.709 H.264 that every browser plays (BUG-19) | +318 / −12 |
| #86 | 18 Oct | **The end product:** edits from the original (C's carry-over), the crop box, the Accounts table with Delete, 60 fps labels, the cold-start note; hls.js preferred over Chrome's own player (BUG-20) | +1920 / −80 |
| #87 | 18 Oct | The image push role trusts GitHub's immutable OIDC subject (BUG-23) | +8 / −2 |
| #88 | 18 Oct | No drift in the base plan: the TLS minimum CloudFront ignores (BUG-24) | +11 / −4 |
| #89 | 18 Oct | The upload player stopped rebuilding every 2 s (BUG-25) | +47 / −5 |

Also on Track A: every staging apply (datastores, CloudFront, registry) and
deploy; the review of D's worker module against `docs/d-worker-datastore-review.md`
before it was applied; reviews and merges of every other track's PRs; the
production data-move rehearsal (production's dump restored into staging's
RDS); the browser end-to-end runs below.

**#86 was built by A at the end of the sprint**, with the user's agreement,
from pieces the plan had given to C (edits from the original) and E (the
crop box, quality labels, the cold-start note, the Delete user button).
They were not in C's or E's PRs when the sprint ended, and the end product
needed them; C's and E's own PRs (#82, #81) are listed under their tracks.

### Track B — Zhang Jizhang (@zhanj384) · upload path and accounts

| PR | What | Size |
|---|---|---|
| #83 | A cap on open upload sessions per user; **`GET` and `DELETE /admin/users/{id}`** — storage first, then rows in one transaction; 403, 404 and 409 (an operator cannot delete themself); direct upload verified on staging S3 | +1125 / −35 |

Not done: files the browser cannot type through the direct path, and
retiring `POST /upload`. Both carry to a later sprint.

### Track C — Yang Dongwei (@ttydw-ch) · media rules and the clip scrubber

| PR | What | Size |
|---|---|---|
| #82 | **The clip scrubber** (`mountClipScrubber`): two handles, `m:ss` readout in tenths up to a minute, the player seeks as a handle moves, arrow keys, `0 ≤ start < end ≤ duration` unbreakable; 35 tests, 100% coverage | +1055 / −5 |

Editing from the original, C's other item, was built by A in #86.

### Track D — Jiang Yibai (@JiangYibai666) · infrastructure

| PR | What | Size |
|---|---|---|
| #84 | **The worker Auto Scaling Group** on contract 2.1 — spot only, five 4-vCPU Graviton types, queue-depth scaling, a daytime minimum of one; the termination hook and `flickpond-drain` (`docker stop --time 100` on a spot notice or scale-in); the FIS interruption experiment; registry state in S3; **the login fix** (password hashing off the event loop); BUG-15 closed (compose default 1080); DAST against staging | +1135 / −185 |

### Track E — Lu Jingxing (@Dilute-l) · the UI

| PR | What | Size |
|---|---|---|
| #81 | **Cancel an upload** in progress (#67); **three parts at a time** (#68); **one resume note per upload** (#69) | +584 / −131 |

The crop box, quality labels, cold-start note and Delete user button were
built by A in #86.

## 3. Measured on AWS staging

| What | Result |
|---|---|
| Datastores from outside AWS | RDS and ElastiCache resolve only to private `172.31.x` addresses; a TCP connection to either from outside AWS times out |
| Video segments | Served by CloudFront (the API answers 307, CloudFront 200). An unsigned CloudFront request and a direct bucket request both get **403** |
| Cold start, pool at zero overnight | Queued at 1 s; "starting a worker" shown at 12 s; **picked up at 3 min 30 s** by an m6g.xlarge spot worker; done 11 s later. About 85 s of the wait is the scale-out alarm's two one-minute samples, about 2 min the instance booting and pulling the image |
| 4K HDR, real clip | 384 MB uploaded in 6 s; MP4 at 5 min 25 s; ladder 360p – 2160p in 13 min 26 s, every rung tone-mapped and labelled 30 fps — under half the 29.5-minute timeout |
| Keep 60 fps | A 6 s 720p60 clip: `FRAME-RATE=60.000` on every rung; the menu reads "720p60", "360p60" |
| Edit from the original | A crop of an edit was measured in the original's 1920 × 1080 and produced from it |
| Account deletion | An operator deleted a test account; it was gone from `/admin/users` |
| Image pipeline | CI built and pushed `flickpond/worker:c52b192…` (arm64, 265 MB) through OIDC — no AWS key anywhere |

## 4. Bug log

Continues from BUG-18 in [`sprint4-report.md`](sprint4-report.md).

### BUG-19 — HDR phone videos came out as a file no browser plays · **critical** · fixed #85

Found by testing a real 4K60 AV1 10-bit HDR10 clip; phones record HDR by
default. The MP4 was H.264 High 10, `yuv420p10le`, still tagged PQ/BT.2020:
no browser plays 10-bit H.264, and HDR shown untreated is flat and grey.
**Fix:** the probe detects PQ and HLG; every encoder tone-maps to BT.709
(`zscale` + Hable) and every chain ends in `format=yuv420p`. Costs about 70%
more ladder time on HDR uploads; SDR pays nothing.

### BUG-20 — Videos did not play in current Chrome · **critical** · fixed #86

Chrome 155 answers "maybe" to `canPlayType('application/vnd.apple.mpegurl')`,
so the page handed the ladder to Chrome's own HLS player, which failed on it
with `MEDIA_ERR_SRC_NOT_SUPPORTED`. Library, upload card and editor were all
affected. 245 jsdom tests passed; found only in a real browser. **Fix:**
hls.js wherever MediaSource exists, the browser's player only where it does
not (iPhone).

### BUG-21 — The crop box could not draw a box · **major** · fixed in #86 before merge

The box starts as the whole frame, so every press landed inside it and was
taken as a move of a box with nowhere to go. Found in the browser test.
**Fix:** a press on a box that fills the frame draws a new one. With it, two
smaller faults: the stage was black on an unplayed video (no frame decoded;
the box now asks for one), and the "60 fps" label could never appear —
FFmpeg writes no `FRAME-RATE` in the master playlist, so the worker now adds it.

### BUG-22 — `edit_options` described the MP4, not what edits start from · **moderate** · fixed #86

With edits starting from the original, a 4K upload was still offered
"upscale to 1080p" — computed from its 720p/1080p MP4. **Fix:** options and
the API's 422 check use the original's size; `edit_source` says which video
that is.

### BUG-23 — CI could not push the worker image · **major** · fixed #87

The role trusted `repo:Flickpond/Media_player:…`, but this repository uses
GitHub's immutable subject claims (`Flickpond@323859155/Media_player@…`), so
`AssumeRoleWithWebIdentity` always failed. Separately, the
`AWS_ECR_PUSH_ROLE_ARN` repository variable had been set with a literal
`<ACCOUNT_ID>` in it. **Fix:** trust the immutable form (also safer: a
renamed or recreated repository cannot inherit the role); correct the variable.

### BUG-24 — The base plan always showed two changes · **minor** · fixed #88

#80 asked CloudFront for `TLSv1.2_2021`, which AWS ignores on the default
`*.cloudfront.net` certificate, recording `TLSv1`. Every plan showed a
change no apply could make — hiding real drift. **Fix:** state what AWS
does. CloudFront therefore does not enforce TLS 1.2 today (§5).

### BUG-25 — The upload player collapsed and expanded every 2 s while HD processed · **moderate** · fixed #89

Reported by the user watching the 4K HDR upload: for the 13 minutes of the
ladder, the player rebuilt on every poll. It was keyed on its URL, and the
MP4's presigned URL has a new signature each time. **Fix:** key on the job
and on MP4-or-ladder; a test where each poll re-signs fails on the old code.

## 5. Known limitations

- **Production is still on Alibaba**, at `735da33` (#80). The cutover
  runbook is rehearsed; the data move was restored into staging's RDS.
- **Overnight, the first job waits about 3½ minutes** for a spot worker.
  A warm pool (sprint 6) should bring it near one minute.
- **CloudFront allows TLS 1.0** on its default certificate. Our own domain
  and certificate fix that (sprint 6, D).
- **Workers share the core's IAM role**, so they can read secrets they never
  use, such as the CloudFront signing key (sprint 6, A).
- **No alarm reaches anyone.** The queue-metrics alarm has no action (sprint 6, D).
- **The FIS spot-interruption test has not run.** Giving a job back on
  SIGTERM is tested in code and by `docker stop` locally (#73), not yet by
  AWS's own two-minute notice (sprint 6, Saturday).
- **Staging's RDS holds a copy of production's data** from the rehearsal;
  it is treated as production data.
- **Browser bugs pass jsdom.** BUG-20, BUG-21 and BUG-25 passed every
  frontend test; a Playwright smoke test is in sprint 6.

## 6. What is left

All of it is in [`sprint6-plan.md`](sprint6-plan.md):

- **The production cutover**, Tuesday 20 October.
- **Four new editor tools**, one per member, on the edit-operation seam
  (merged as #91): rotate and speed (B), colour (C), the seek-bar preview
  (D), filter presets (E).
- **Chunked MP4 encoding, phase 1**, a warm pool and a worker role of its
  own (A).
- CloudFront on our own domain, alerts, the FIS test (D).
- From this sprint, not yet scheduled: files the browser cannot type
  through the direct path, and retiring `POST /upload` (B); Keep HDR; admin
  roles and rights.
