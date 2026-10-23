# Sprint 6 — Team Record (final)

**Written:** 24 October 2026 · figures from `main` @ `4af6033` (#119) plus the
open #129; measurements on flickpond.com, 19–24 October
**Sprint window:** Monday 19 – Saturday 24 October 2026. There is no sprint 7:
Saturday 24 to Wednesday 28 is debugging time, fixes only.
**Status:** **flickpond.com runs on AWS** since the cutover on 19 October. All
five tracks' features are merged and live behind their switches; every new
edit operation was run on the live site. One of D's pieces waits on a
person: the email addresses for the alerts.
**Plan:** [`sprint6-plan.md`](sprint6-plan.md) — one feature per member on a
shared edit-operation seam, everything behind a switch.

---

## 1. Where the project stands

| | |
|---|---|
| Deployed at | **<https://flickpond.com>** on AWS `ap-southeast-1` since 19 Oct: core `t4g.small`, RDS PostgreSQL, ElastiCache Valkey, S3 + CloudFront, spot workers 0–4. Alibaba `47.238.64.156` kept untouched as the rollback until 26 Oct |
| Core | `main` @ `4af6033`, deployed 23 Oct with the *Deploy core* workflow (manual, reviewer approval, no SSH key) |
| Workers | image `4af6033…`; switches below |
| Tests | **984 Python** (Postgres suite in CI), **333 frontend**, ruff clean; a real-Chromium smoke test of the whole stack runs on every PR |
| Merged this sprint | **38 PRs**, #90 – #128 (#114 was folded into #115), from all five tracks |
| Longest upload | **10 minutes** (was 5) |

**Switches on flickpond.com**

| Switch | State | Where |
|---|---|---|
| `WORKER_CHUNKED_MP4` — MP4 in pieces across workers | on | `infra/workers` `worker_settings` |
| `WORKER_CHUNKED_LADDER` — HD ladder in pieces | on | same |
| `WORKER_KEEP_HDR` — an HDR rendition for HDR screens | on | same |
| `AV1_POPULAR_VIEWS` — AV1 after N plays | 3 | core `.env` → API |
| `MEDIA_MAX_DURATION_SECONDS` | 600 | workers and API |
| `TRASH_RETENTION_DAYS` — library trash | 30 | API and reaper |
| `WORKER_PREVIEW_SPRITE` — seek-bar preview (D) | **on** since 24 Oct, checked live | `infra/workers` |
| `cdn_alias` — `cdn.flickpond.com` (D) | **on** since 24 Oct: video segments served from `cdn.flickpond.com` | `infra/terraform` |
| `infra/alerts` — email alerts (D) | not applied; waits on the list of addresses | `infra/alerts` |

**What a user gets since this sprint** (each checked on flickpond.com):

- **Faster long videos.** A 4:55 4K HDR upload's MP4 in 4 minutes and its
  whole HD ladder 7½ minutes later (21½ on one worker); a 9:55 one, MP4 in
  6½ minutes, ladder in 13½.
- **Uploads up to 10 minutes.**
- **New edits:** rotate, flip and speed (B), colour sliders (C), five
  presets (E), on top of crop, clip, scale and convert — every one run on
  the live site, alone and all together.
- **Crop to an exact size** — type 1000 × 1000.
- **After an edit: keep both, or delete the original.**
- **A trash:** a deleted video waits 30 days; Restore or Delete forever.
- **Previews for edits**, and a music note on MP3 cards.
- **HDR screens get HDR, AV1 browsers get AV1** for popular videos.
- **Fullscreen fills the screen.**
- **A preview frame when hovering the seek bar** (D).
- **Progress while processing** ("Processing… 42%").

## 2. Who built what

### Track A — Ibrahim Mammadov (@1brahim74) · processing, AWS, and the final week

- **The cutover** to AWS (#93, run 19 Oct) with the runbook and rollback.
- **The edit-operation seam** (#91) every other track plugged into.
- **Chunked MP4** (#95, #102, made frame-exact in #115) and **the HD ladder
  in pieces** (#115, #116, #117).
- **Keep HDR and AV1 for popular videos** (#105, #108–#112).
- **Deploy from CI** (#107), **browser smoke test in CI** (#106), smaller
  worker image (#103), worker wake-up and cold start (#94, #102).
- **User features from the last days:** trash (#127), keep or delete the
  original (#126, #128), typed crop size (#125), MP3 card (#124), edit
  previews (#123), 10-minute uploads (#118), fullscreen (#113), paged
  accounts (#104), progress (#98), thumbnails a tenth of the way in (#129).
- Merged B's and C's branches with E's and A's work (#121, #122) and chained
  D's migration after the trash's (#119).

### Track B — Zhang Jizhang (@zhanj384) · rotate, flip and speed

#121 (+841): rotate 90/180/270, horizontal/vertical flip, 0.5×–2× speed with
the audio's pitch kept, each with its own editor panel and live preview, on
the seam. Real-FFmpeg tests for dimensions, duration and pitch.

### Track C — Yang Dongwei (@ttydw-ch) · colour adjustments

#122 (+1,313): brightness, contrast, saturation and gamma — four sliders, a
live preview matched to FFmpeg's `eq`, applied after the tone-map so HDR and
SDR uploads mean the same. Strict ranges, every field optional.

### Track D — Jiang Yibai (@JiangYibai666) · seek-bar preview, alerts, CDN

#119 (+2,382), everything behind a switch: a sprite sheet and WebVTT index
made after the ladder and shown when hovering the seek bar; email alerts for
the site down (Route 53 health check), queue metrics missing and the budget
at US$80; and CloudFront on `cdn.flickpond.com` with TLS 1.2 as the floor.

### Track E — Lu Jingxing (@Dilute-l) · filter presets

#120 (+2,000): B&W, Warm, Cool, Vintage and Vivid, previewed in WebGL from
colour tables generated from the worker's own FFmpeg filters, with a
labelled CSS fallback; 15 WebGL-versus-FFmpeg comparisons.

## 3. Measured on flickpond.com

| What | Result |
|---|---|
| **4:55 4K 59.94 fps HDR**, MP4 | ready **4 min** after upload, chunked on 4 workers |
| Same clip, HD ladder 360p – 2160p | **7 min 27 s** in pieces; **21 min 22 s** on one worker |
| **9:55 4K HDR** | MP4 in **6 min 31 s**; ladder **13½ min** — and **31½ min** on a run during which AWS reclaimed 3 spot workers (AWS's activity log); it finished correctly either way |
| Chunked MP4 against one encode | 9:55 clip: **17,850 frames, every one the same picture** (lowest 37.1 dB); no seams across 59 joins. Locally also 24, 25, 29.97, 50, 60 fps, portrait and variable frame rate |
| Ladder in pieces against one run | every rung (360p – 2160p) the same frames and duration, audio identical, no gap or overlap between 100 segments; plays across joins at 2160p with 0 dropped frames |
| Helper workers from 1 to 4 | **20–40 s** after an upload |
| AV1 for popular videos | AV1 1080p peaks at **3.4 Mbps** against H.264's 6.0; Chromium starts in AV1 |
| New edits | rotate → 1080×1920; flip, colour, B&W preset → 1920×1080; speed 1.5× → 70 s became 46.7 s; all eight operations at once → 540×720, 35 s, MKV |
| Trash | delete → hidden from library, page and player; restore → back; Delete forever → **12 output files and the original gone from S3** |
| `cdn.flickpond.com` (D) | TLS 1.1 refused, TLS 1.2 on our own certificate (verified), unsigned request 403 from CloudFront; a played segment's link redirects to `cdn.flickpond.com` (200). The old `*.cloudfront.net` name turned out to refuse TLS 1.1 as well |
| Seek-bar preview (D) | 51 s after a 70 s upload: `preview.vtt` 200, 15 cues (one per 5 s), the sheet (1600 × 180) through CloudFront, a frame shown on hovering the bar. Not checked: that the frame matches the moment hovered |
| Worker image | 1.05 GB → **787 MB** |

## 4. Bug log

Continues from BUG-25 in [`sprint5-report.md`](sprint5-report.md). Every one
of these was found by testing on the live site or against real FFmpeg, not
by the unit tests — which is the main lesson of the sprint.

### BUG-26 — No worker image was built for a week of merges · **major** · fixed #101
A SonarCloud gate failure from #95 stopped CI before the image step; workers
silently kept running old code.

### BUG-27 — Chunk helpers arrived too late to help · **major** · fixed #102
The second worker joined 3½ minutes in. The coordinator now asks for its
helpers' workers itself.

### BUG-28 — HDR and AV1 renditions were never played · **major** · fixed #111
hls.js starts in a codec family only if its cheapest entry fits its first
bandwidth estimate (~500 kbps); uncapped AV1 started at 5.8 Mbps and HDR at
13.3. Capped rungs down to 360p.

### BUG-29 — The AV1 switch never reached the API · **moderate** · fixed #108/#110
Compose lists the API's environment explicitly; a value in `.env` alone is
never seen.

### BUG-30 — HDR and AV1 jobs dropped each other's playlist entries · **major** · fixed #112
Two workers updated the same master playlist at once; the last writer won.
Now re-read and written under a per-video lock.

### BUG-31 — Fullscreen showed a 360-pixel strip · **moderate** · fixed #113

### BUG-32 — Chunked MP4 pieces were 1/60 s off · **moderate** · fixed in #115
At 60 → 30 fps each piece rounded frames on its own clock; a scene cut came
a frame early. Pieces now keep each frame's own timestamp, start on a frame
that begins an output frame, and stop by frame count. Three more faults were
found and fixed while proving it (24/25 fps came out at 30; an exact-half
frame; a 60 fps piece losing its last frame).

### BUG-33 — A 5-minute 4K upload never got its HD versions · **major** · fixed in #115
The ladder had the MP4's 30-minute limit and timed out. It now has an hour,
and is encoded in pieces.

### BUG-34 — AV1 jobs queued by the API had 15 minutes · **minor** · fixed in #115

### BUG-35 — The HD playlist was published before its segments · **moderate** · fixed #116

### BUG-36 — The ladder in pieces stored every video twice · **moderate** · fixed #117
Its working archives were uploaded beside the ladder (~590 MB for a 5-minute
4K video).

### BUG-37 — Edits had no preview picture · **minor** · fixed #123

### BUG-38 — Clips that open on black got black previews · **minor** · fixed #129

### BUG-39 — Two migrations would have stopped the API · **critical, caught before merge** · fixed in #119
D's and the trash's migrations were both "07" after "06"; with both merged
`alembic upgrade head` fails, and the API runs it on start. D's became 08.

## 5. Known limitations

- **Spot interruptions can double a long ladder** (31½ instead of 13½
  minutes). It finishes correctly; one on-demand worker or more instance
  types would make it rarer.
- **Not tested:** a real HDR screen; Safari and phones (out of scope — the
  project targets PC browsers).
- **The trash's automatic emptying** is unit-tested but cannot be seen live
  before 22 November; Delete forever, the same code, was checked on S3.
- **A video deleted while still processing** can leave its MP4 and preview
  in S3: they are written after the row is gone.
- **"720p first"** (publishing low rungs before high ones) was dropped: the
  chunked MP4 already plays at up to 1080p within minutes.
- **The library does not refresh after a delete made elsewhere**, so a stale
  card can say "not found".

## 6. What is left

| Item | Who | When |
|---|---|---|
| Choose the alert addresses; apply `infra/alerts`; everyone confirms two emails | team, then A | 24–28 Oct |
| Final dump of the Alibaba host and shut it down | A | after 26 Oct |
| Delete the test account `cutover-check-…@example.com` with its videos | operator | any time |
| Make the browser smoke test a required check on `main` | Ibrahim | any time |
