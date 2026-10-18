# Sprint 6 — Plan

**Window:** Monday 19 October – Sunday 25 October 2026  
**Freeze:** Friday 23 · **Verify on production:** Saturday 24 · **Report:** Sunday 25  
**Written against:** `main` @ `77c6185` · from [`roadmap.md`](roadmap.md) and the
sprint 5 results

**Sprint goal:** flickpond.com runs on AWS, and the editor grows four new
tools — rotate and speed, colour, filter presets, and a preview on the seek
bar — each built end to end by one person. The first upload of the morning no
longer waits minutes for a worker, and a long video's MP4 is encoded in
parallel across workers.

One member, one feature, end to end. Weekends count as working days. **Read
[`known-traps.md`](known-traps.md) before writing code.**

---

## Final week — decided Monday 19 October

**There is no sprint 7. Saturday 24 October is the end of the project.** Every
item that was planned for later is attempted this week instead, on one rule:

> **Everything new ships behind a switch.** Whatever is not proven on
> flickpond.com by **Friday 23, 20:00 (freeze)** is switched off or rolled
> back, and goes into the final report as future work. The site must work on
> Saturday whatever happens.

flickpond.com moved to AWS on **Monday 19 October** (the cutover planned for
Tuesday, done a day early). Verify everything on the live site.

| Track | Added this week, on top of the sections below | Switch |
|---|---|---|
| **A** | Chunked MP4 timed on spot workers and switched on; the HD ladder in pieces with **720p first**; **Keep HDR** (taken over from C); **encode by popularity + AV1**; smaller worker image; paged accounts; Playwright smoke test in CI; deploy from CI (manual trigger only) | `WORKER_CHUNKED_MP4`, and a flag per feature |
| **B** | Rotate/flip/speed, then **admin roles**: a moderator role and an audit log of deletions | revert |
| **C** | Colour adjustments (Keep HDR moves to A) | section hidden |
| **D** | CloudFront on `cdn.flickpond.com`, alerts, the spot-interruption test **by Thursday** | apply / revert |
| **E** | Filter presets, and a browser test of the editor | section hidden |

**Saturday 24:** verification on flickpond.com, the demo, and the final report.

---

## 0. Who has what

| Track | Owner | This sprint | Main deliverable |
|---|---|---|---|
| **A** | Ibrahim Mammadov (@1brahim74) | Processing, AWS, the editor's plug-in seam | The cutover; the seam (Monday); chunked MP4 encoding, phase 1; warm pool and a worker role of its own |
| **B** | Zhang Jizhang (@zhanj384) | **Rotate, flip and speed** | Three new edit operations: rotate 90/180/270, mirror, 0.5×–2× with the audio pitch kept |
| **C** | Yang Dongwei (@ttydw-ch) | **Colour adjustments** | Brightness, contrast, saturation and gamma sliders, previewed live in the browser, applied by FFmpeg |
| **D** | Jiang Yibai (@JiangYibai666) | **Seek-bar preview** and infrastructure | Hovering the seek bar shows that moment's frame; CloudFront on our own domain; alerts that reach someone |
| **E** | Lu Jingxing (@Dilute-l) | **Filter presets** | One-click looks (B&W, warm, cool, vintage, vivid) previewed live, applied by FFmpeg |

**Each feature is yours from the FFmpeg filter to the button.** It lives in
its own files, plugs into the editor through the seam in 2.1, and cannot
block anyone else's — or be blocked by anyone else's after Monday.

---

## 1. Decisions already made

- **The cutover happens first, Tuesday evening.** Everything it needs was
  proven on staging in sprint 5: RDS and ElastiCache private and TLS-only,
  CloudFront serving segments, spot workers scaling from zero, a 4K HDR
  upload laddered in 13 min 26 s. The DNS TTL is already 10 s. A and D.
- **New edit operations plug into one seam** (2.1). A builds it on Monday
  with a template operation and its tests; after that, adding an operation
  touches no one else's code beyond a one-line registration.
- **A fixed order, whatever the request lists:**
  `clip → crop → rotate/flip → scale → colour → preset → speed → convert`.
  Crop stays in the original's pixels because it runs before rotation.
  Colour and presets run after the HDR tone-map, so they always see standard
  8-bit video.
- **The live preview is the browser's, the result is FFmpeg's.** While a
  slider moves, a CSS filter (C) or a WebGL shader (E) changes the picture
  on the page; nothing goes to the server until **Process**. How closely the
  preview matches the result is part of each feature's acceptance.
- **Chunked encoding is two sprints.** Phase 1 (this sprint): the MP4 only,
  split into keyframe-aligned pieces of about 10 s, encoded on the existing
  worker pool, joined without re-encoding; behind `WORKER_CHUNKED_MP4`, off
  by default. Phase 2 (sprint 7): the ladder.
- ~~A warm pool of one stopped worker~~ — **changed 19 Oct (#94):** AWS warm
  pools do not support Spot in a mixed-instances group. The 3 min 30 s wait
  is cut directly instead: the API wakes the group from zero as a job is
  queued (the alarm's ~85 s), and workers boot the ECS-optimized image with
  Docker preinstalled.
- **Workers get their own IAM role.** Today they share the core's and can read
  secrets they never use, such as the CloudFront signing key.

---

## 2. The contracts — A posts 2.1 on Monday morning

### 2.1 The edit operation seam (A builds it, everyone uses it)

Each operation is one module under `app/worker/edit_ops/` and one schema
entry, registered in one line:

| Part | Where | What it holds |
|---|---|---|
| Parameters | `app/schemas/edit.py` | A strict Pydantic model, added to the `EditOperation` union |
| Rule | `app/services/media_rules.py` | What the API refuses before queueing (422), and the worker repeats against the real file |
| Filter | `app/worker/edit_ops/<name>.py` | `build(params, frame) -> (video_filters, audio_filters, frame_after)` |
| Panel | `frontend/<name>.js` + `.css` | `window.mount<Name>(video, onChange)`, the same contract as the crop box and the clip scrubber ([`EDITOR-COMPONENTS.md`](../frontend/EDITOR-COMPONENTS.md)) |

`frame_after` is the frame size the operation leaves behind, so a scale after
a 90° rotation is judged against the rotated frame. A missing panel disables
its section with "Coming in the next update", as before.

**The parameters, agreed now so nobody waits on anybody:**

```jsonc
{ "operation": "rotate",  "params": { "degrees": 90 } }               // 90 | 180 | 270
{ "operation": "flip",    "params": { "axis": "horizontal" } }        // horizontal | vertical
{ "operation": "speed",   "params": { "factor": 1.5 } }               // 0.5 – 2.0, steps of 0.25
{ "operation": "adjust",  "params": { "brightness": 0.1, "contrast": 1.2,
                                       "saturation": 1.1, "gamma": 1.0 } }
{ "operation": "preset",  "params": { "name": "warm" } }              // bw | warm | cool | vintage | vivid
```

`adjust` ranges: brightness −0.3…0.3, contrast 0.5…2, saturation 0…3, gamma
0.5…2. Every field is optional; leaving one out leaves that value alone.
`speed` with `clip`: the clip is cut on the original's timeline first.

### 2.2 The seek-bar preview (D builds it, A adds the hook)

On Monday A adds an empty `after_ladder` hook to the ladder job. D's module
fills it: one frame every 5 s, 160 px wide, tiled into
`outputs/<job>/preview/sprite.jpg`, with a WebVTT index beside it. The API
adds `preview_vtt_url` (absent until the sprite exists) to the job shape.
The player turns on Plyr's own `previewThumbnails` when the field is there.
A failed preview never fails the job.

### 2.3 Chunked MP4 (A builds it, nobody else depends on it)

The job row does not change. With `WORKER_CHUNKED_MP4=true`, a video longer
than 60 s is split into chunks, each queued as its own job on
`video_jobs-chunks`, and joined by the worker that finishes the last one.
`status` and `output_key` are still written only through
`app/repositories/jobs.py`. With the flag off, nothing changes.

---

## 3. How work flows this week

1. **Contracts by Monday noon.** A posts 2.1–2.3 in the group. From then on
   you build against the document, not against someone's branch.
2. **Stand-ins, not waiting.** If your feature needs something not yet
   merged, write a small default behind a flag or a missing-component note,
   and say so in the PR. The real piece replaces it when it lands.
3. **One PR a day, small.** Open or update your PR by the end of each day.
   Branch names: `<track>-sprint6-<topic>`, e.g. `c-sprint6-colour`.
4. **Review within 24 hours.** Ibrahim reviews and merges in the evening.
   A's own PRs have a named reviewer per item (below); after 24 hours any
   teammate may approve.
5. **Check-in Wednesday evening.** Each track answers in the group: a draft
   PR link, and "on track" or "need help".
6. **Take-over Thursday morning.** A feature with no draft PR and no answer
   by then moves: a free teammate or A picks it up, or it moves to sprint 7.
   It never holds up anyone else's merge. The report records who started it,
   who finished it and why.
7. **Verify on staging, then production.** Every feature is checked by its
   owner on https://47-128-86-77.sslip.io before the freeze, and on
   flickpond.com on Saturday.

| A's PR | Reviewer |
|---|---|
| The seam (2.1), the preview hook (2.2) | C |
| Chunked MP4 (2.3) | B |
| Wake on enqueue (was: warm pool), worker role | D |

---

## 4. Schedule

| | Mon 19 | Tue 20 | Wed 21 | Thu 22 | **Fri 23 · freeze** | **Sat 24 · verify** | Sun 25 |
|---|---|---|---|---|---|---|---|
| **A** Ibrahim | Sprint 5 report. **Post 2.1–2.3 by noon.** The seam: registry, fixed order, `frame_after`, a template operation with tests; the empty `after_ladder` hook. | Cutover rehearsal on staging in the morning. **Cutover in the evening** with D: stop writes, `pg_dump` → RDS, MinIO → S3, the A record at Spaceship. | Warm pool; the worker role. Chunked MP4: split on keyframes, chunk jobs. | Chunked MP4: join without re-encoding, the last chunk triggers it; a stopped chunk worker gives its chunk back. | PR complete. A 10-minute video, chunked vs. not, timed on 4 spot workers. | Verify on production; flag stays off unless the timing and the seams pass. | Sprint 6 report |
| **B** Zhang | Read 2.1. `rotate` and `flip`: schemas, rules (a 90° rotation swaps width and height). | `rotate`/`flip` filters on the template; tests including rotate + crop + scale. | `speed`: `setpts` and `atempo` (chained for 0.5×), with clip. **Check-in.** | Panel: rotate buttons with a CSS preview, mirror toggles, speed dropdown. | PR complete; verified on staging | Verify on production | Slack |
| **C** Yang | Read 2.1. `adjust`: schema, ranges, rules. | `eq` filter on the template, after the tone-map; tests. | Panel: four sliders with a live CSS-filter preview; Reset. **Check-in.** | Match the preview to FFmpeg: compare stills at five settings, tune the mapping. | PR complete; verified on staging | Verify on production | Slack |
| **D** Yibai | Read 2.2. Production apply of the worker group and CloudFront, with A. | **Cutover** with A: workers live; arm the health alarm. | Seek-bar sprite and WebVTT in the hook. **Check-in.** | `preview_vtt_url` in the API; Plyr `previewThumbnails`. CloudFront on `cdn.flickpond.com` (ACM in us-east-1, TLS 1.2 minimum). | PR complete. Alerts to an SNS email topic: queue metrics missing, health, budget at $80. | Verify on production. Spot interruption test (AWS FIS) with A. | Write-up: the first week's real AWS cost |
| **E** Lu | Read 2.1. `preset`: schema; the five looks as FFmpeg chains (`eq`, `colorbalance`, `curves`). | Filters on the template; tests. | Panel: preset tiles with a live WebGL preview on the video. **Check-in.** | Match the preview to FFmpeg, as C does; one Playwright smoke test of the editor in a real browser. | PR complete; verified on staging | Verify on production | Slack |

### Checkpoints — things you can check, not statuses you post

| By the end of | This is true, or we have a problem |
|---|---|
| Mon 19 | 2.1–2.3 posted. The template operation merged, with tests. The sprint 5 report merged. |
| Tue 20 | flickpond.com is served from AWS. A 4K upload plays in 4K. Alibaba is untouched, kept for a week as the rollback. |
| Wed 21 | Every track has a draft PR (the check-in). The first job after an idle hour starts in under 1.5 min. |
| Thu 22 | Each new operation works through the API on staging, without its panel if need be. Take-overs, if any, are decided. |
| Fri 23 | **Freeze:** every track's PR is complete and verified on staging. A chunked 10-minute MP4 has no visible seams. |
| Sat 24 | On flickpond.com: rotate + speed, colour, a preset and the seek-bar preview each work on one real upload; a spot worker interrupted mid-job gives the job back; alerts reach the email topic. |

---

## 5. Carried over, folded in above

- **The production cutover** — sprint 5's Saturday, not yet run — A and D, Tuesday.
- **The sprint 5 report** — A, Monday.
- **The spot interruption test with AWS FIS** — D and A, Saturday.
- **CloudFront on our own domain** — today's `*.cloudfront.net` certificate
  allows TLS 1.0 whatever is asked (#88) — D.
- **Alerts that reach someone** — the queue-metrics alarm has no action — D.
- **A worker role of its own** — found in the workers review — A.
- **Browser tests in CI** — three bugs in sprint 5 passed 245 jsdom tests and
  only showed in Chromium (#86, #89) — E starts with one smoke test.

## 6. Not this sprint

- **Admin page with real roles and rights** — sprint 7, with B (accounts).
- **Keep HDR** — an HDR ladder beside the SDR one — sprint 7, with C.
- **Chunked encoding, phase 2: the ladder** — sprint 7, A.
- **Encode by popularity, AV1** — after chunked encoding.
- **Deploys from CI** — after the cutover has run for a week.

## 7. Rules that have not changed

1. `main` requires a pull request and a review.
2. Ask before editing a file another track owns, and say so in the PR. The
   one-line registrations in 2.1 are agreed here and need no asking.
3. Every file under `tests/integration/` needs the `RUN_POSTGRES_TESTS` guard.
4. The worker is the sole writer to `status`, `output_key`, `error` and
   `updated_at` after the API's insert, through `app/repositories/jobs.py` only.
5. Never commit credentials. Check `git diff --cached --name-only` first.
   Secrets live in SSM Parameter Store.
6. Anything that opens a datastore to the network is reviewed before
   `apply`, by someone other than whoever runs it.
7. After the cutover, [`aws-cutover.md`](aws-cutover.md) is the deploy
   procedure, and CLAUDE.md's deployment section is rewritten to match.
