# Sprint 3 — Plan

**Window:** Friday 18 September – Friday 25 September 2026
**Written against:** `main` @ `9922ad0`

**Sprint goal: make the video actually watchable, and let people edit it.**

Two features, one sprint:

1. **Adaptive HLS playback** — three quality levels per video, the player
   picks one based on the network. Right now every video is a single 720p
   MP4 and a slow connection just buffers.
2. **The editing suite** — crop, clip, downscale, upscale and convert, all
   five, configured in the browser and run in one FFmpeg pass. Designed in
   full in [`video-editing-plan.md`](video-editing-plan.md).

Everything else this sprint either serves those two or stays out of the way.

**Read before writing code:** [`known-traps.md`](known-traps.md) (24 traps
already hit here — most fail silently), [`contract.md`](contract.md), and
your own section below.

---

## 0. Who has what

| Track | Owner | This sprint | Effort |
|---|---|---|--:|
| **A** | Ibrahim Mammadov (@1brahim74) | HLS ladder + delivery, the editing backend (all five operations), retry | ~6 d |
| **B** | Zhang Jizhang (@zhanj384) | The crop box — drag a rectangle over a paused frame, convert it to source pixels | ~3 d |
| **C** | Yang Dongwei (@ttydw-ch) | The clip scrubber — pick a start and end on the timeline | ~2 d |
| **D** | Jiang Yibai (@JiangYibai666) | DevSecOps: SAST, DAST, the 50-user load test, Terraform | ~5 d |
| **E** | Lu Jingxing (@Dilute-l) | The player (Plyr + hls.js) and the editing panel B and C plug into | ~4 d |

**B and C are frontend this sprint.** The crop box and the clip scrubber are
the two genuinely new interactive components in the whole project, and each
is a self-contained piece of work with a clear interface — which is exactly
what makes them good standalone tracks. Track E owns the panel they sit in;
Track A owns the backend that consumes what they produce.

File ownership otherwise carries over from sprint 1 (`CLAUDE.md`). Ask in
the group before editing a file another track owns.

---

## 1. The contract between the four tracks that touch editing

Everything in the editing feature meets at one request body. Agree on it
Friday and nobody has to wait for anybody:

```
POST /jobs/{id}/edit
  { "operations": [
      { "operation": "crop",      "params": { "x": 0, "y": 140, "w": 1080, "h": 1080 } },
      { "operation": "clip",      "params": { "start": 5.0, "end": 12.5 } },
      { "operation": "downscale", "params": { "height": 480 } },
      { "operation": "upscale",   "params": { "height": 1080 } },
      { "operation": "convert",   "params": { "format": "mkv" } }
    ] }
  -> 202 { "job_id": "<uuid>" }
```

Any non-empty subset. The **order in the array does not matter** — the
worker always runs clip → crop → scale → convert, because crop's
coordinates are anchored to the original frame and shrinking before scaling
is also the cheaper order. `downscale` and `upscale` together is a 422.

**The two component interfaces**, so B and C can build without waiting on E:

| Component | Owner | Produces |
|---|---|---|
| Crop box | B | `{ x, y, w, h }` in **source pixels**, not screen pixels |
| Clip scrubber | C | `{ start, end }` in **seconds**, floats, `start < end` |

Each is a function that takes a `<video>` element and a callback, and calls
the callback whenever the user changes the selection. Nothing else. E
mounts them; A validates what they produce against the real file.

---

## 2. Schedule

| When | What |
|---|---|
| **Fri 18 Sep** | Track A merges the migration (two columns, §3.1) and posts the `/edit` contract. Everyone else starts. |
| **Sat 19 – Sun 20** | Build. A's HLS ladder producing files locally by Sunday night. |
| **Mon 21** | Everyone posts one line in the group: what's merged, what's blocked. |
| **Tue 22 – Wed 23** | Build. **Wed 23 is feature freeze** — everything you're shipping is in an open PR by end of day. |
| **Thu 24** | Review, merge, deploy to flickpond.com and **verify there**, not just locally. |
| **Fri 25** | Sprint 3 report and retrospective. |

The Thursday deploy is the real deadline. A PR that merges Friday morning
did not ship this sprint.

**There is one migration this sprint and Track A writes it** — both new
columns are A's. Nobody else opens one; if you think you need one, say so in
the group first.

---

## 3. Track A — the platform everything else plugs into

**Owner: Ibrahim (@1brahim74).** ~6 days, and the biggest single load in the
sprint. In priority order.

### 3.1 The migration (day 1, ~0.25 day)

```sql
ALTER TABLE jobs ADD COLUMN operations JSONB;   -- what an edit job was asked to do
ALTER TABLE jobs ADD COLUMN hls_key    TEXT;    -- the master playlist, when one exists

ALTER TABLE jobs ADD CONSTRAINT ck_jobs_operations_nonempty_array
  CHECK (operations IS NULL OR
         (jsonb_typeof(operations) = 'array' AND jsonb_array_length(operations) > 0));
```

Both nullable, so nothing existing needs migrating. Merge it first —
everyone's model code reads these.

### 3.2 HLS adaptive playback (~2.5 days)

One extra FFmpeg invocation after the existing transcode, producing three
renditions and a master playlist **alongside** the MP4, not replacing it. If
the ladder fails, existing playback and download are untouched.

```
outputs/{job_id}/hls/master.m3u8      <- hls_key points here
outputs/{job_id}/hls/v0/seg00001.ts   <- 360p
outputs/{job_id}/hls/v1/...           <- 480p
outputs/{job_id}/hls/v2/...           <- 720p
```

Built with `-var_stream_map` and `-master_pl_name`, and **capped by the
source's real height** — a 480p upload gets two renditions, not an upscaled
720p one it has no detail for. That means `ffprobe` on the source first,
which the editing suite needs anyway.

**Delivery is the hard part.** Every output today is one presigned MinIO URL.
A ladder is hundreds of objects and a player can't ask for each to be signed
in advance. Making the prefix public-read would work in an afternoon and
re-create P1 — the finding sprint 1's whole review existed to fix. Instead:

```
GET /api/jobs/{id}/hls/{path}   -> check ownership -> 307 to a fresh presigned URL
```

FFmpeg writes relative segment names, so the player resolves them back
through this same route and the whole ladder works with **no manifest
rewriting**. New file, `app/api/hls.py`. `{path}` is attacker-controlled and
goes into an object key — validate it like `safe_filename` already validates
uploads.

- [ ] A processed video has an `hls_key` and a master playlist with three variants.
- [ ] A 480p source never gets an upscaled rendition.
- [ ] Another owner's manifest is 404, and `../` in the path is 404.
- [ ] MP4 playback and download are unchanged for every existing job.

### 3.3 The editing backend — all five operations (~3 days)

`POST /jobs/{id}/edit` per §1, and the worker registry behind it.
`get_processing_step()` returns one cached step today, which can't carry
per-job operations — so a second, per-job path is added beside it rather
than bending the existing one.

| Operation | FFmpeg | Needs `ffprobe`? |
|---|---|---|
| `clip` | `-ss {start} -to {end}` (input-side, so it bounds every later step) | Yes — validate against duration |
| `crop` | `crop={w}:{h}:{x}:{y}` | Yes — validate the rect fits the frame |
| `downscale` | `scale=-2:{height}` | No |
| `upscale` | `scale=-2:{height}:flags=lanczos` | No |
| `convert` | remux (`-c copy`) or `-vn -c:a libmp3lame` | No |

**One pass, one output.** Five operations do not mean five re-encodes — that
would be five generations of loss. Everything composes into one command.

- [ ] Any non-empty subset works, in any order the client sends it, executed in the fixed order.
- [ ] `downscale` + `upscale` together is 422, not a silent pick.
- [ ] A crop rect outside the frame, or a clip past the end, fails with a readable message — never a crash mid-encode.
- [ ] All five operations in one job shell out to `ffmpeg` **once**.

### 3.4 Retry (~0.5 day)

A failed job is currently dead forever — the only recovery is re-uploading
the file. Once people start running edits that can fail on bad parameters,
that gets old fast.

`POST /jobs/{id}/retry`: a conditional `UPDATE … WHERE status = 'failed'`
back to `queued`, clearing `error` and `output_key`, then re-enqueue. No
schema change. Two concurrent retries can't both win — same race-safe
pattern `mark_processing` already uses.

---

## 4. Track B — the crop box

**Owner: Zhang Jizhang (@zhanj384).** ~3 days. The single most interesting
piece of UI in this project.

A draggable, resizable rectangle over a paused video frame, which reports
its selection **in the source video's real pixel coordinates** — not the
on-screen ones. That conversion is the whole problem: the `<video>` element
is displayed at whatever size the layout gives it, letterboxed, on a display
that may not be 1:1, while `crop=w:h:x:y` needs real pixels.

- Use a small library (**cropperjs**) rather than hand-rolling drag maths.
- Optional aspect-ratio lock (1:1, 16:9, free) is worth having and cheap once the box exists.
- Preview is **client-side only** — no request is sent while the user drags. That's the entire reason this feature needs no undo.

**Test against a source that is not 16:9 and not square** — a vertical phone
video, say. A naive screen-pixel-to-source-pixel mapping looks perfect on a
16:9 desktop clip and is wrong everywhere else.

- [ ] The reported rectangle, converted back to source pixels, matches where a human would say they dragged it.
- [ ] Correct on a vertical (9:16) source, not just a landscape one.
- [ ] The box cannot be dragged outside the frame or resized to zero.
- [ ] Nothing is sent to the server while dragging.

---

## 5. Track C — the clip scrubber

**Owner: Yang Dongwei (@ttydw-ch).** ~2 days — the smallest track, on
purpose.

A trim range over the video's timeline: two handles, a highlighted region
between them, and a readout in `m:ss`. Previewed against the video already
loaded in the player, so scrubbing costs nothing.

- Emits `{ start, end }` in seconds. `start < end` always — the UI enforces it, and A's endpoint enforces it again.
- Clicking inside the selected range seeks the player there, so the user can actually watch what they picked.
- The handles need to work with the mouse **and** the keyboard (arrow keys, once focused).

- [ ] `start >= end` is impossible to produce by dragging.
- [ ] The readout matches where the player actually seeks.
- [ ] Works on a 10-second clip and a 10-minute one — the scrubber is proportional, not fixed-pixel.

**If you finish early**, take `POST /jobs/{id}/retry` (§3.4) off Track A's
plate — it's small, self-contained, and A's is the heaviest track.

---

## 6. Track D — DevSecOps

**Owner: Jiang Yibai (@JiangYibai666).** ~5 days. Unchanged from the
previous draft, and deliberately independent: this track blocks nobody and
nobody blocks it.

- **SAST** — a scan as a CI job, gating on zero critical findings the way Trivy already gates the image scan. SonarQube if a hosted account is practical; `bandit` + ruff's security rules if not. **Say which you actually used** in the report.
- **DAST** — an OWASP ZAP baseline scan against the stack running in CI, failing on high-severity findings.
- **Load test** — 50 concurrent users (`locust` or `k6`), real numbers into `scaling-notes.md` with the command used, so anyone can re-run it.
- **IaC** — Terraform describing the current Alibaba ECS deployment: instance, security group, DNS. `terraform plan` matching what's actually running is the deliverable; migrating the live deploy can wait.

**One thing to watch:** Track A's HLS route serves many small files per
video instead of one big one. If the load test or the deploy shows nginx
needs tuning for that, `deploy/nginx/locations.conf` is your file —
coordinate with A.

---

## 7. Track E — the player and the panel

**Owner: Lu Jingxing (@Dilute-l).** ~4 days. Two pieces.

### 7.1 Plyr + hls.js (~2.5 days)

Every video in the app plays through a bare `<video controls>` today: dated,
inconsistent across browsers, no speed control, no quality selector. Wrap it
in **Plyr** (no bundler needed, themes through the CSS variables
`style.css` already defines), then wire **hls.js** in — Safari plays HLS
natively, Chrome and Firefox don't.

- Play `hls_key` through hls.js when a job has one; **fall back to the MP4 when it doesn't.** Every existing job has no ladder and must still play.
- Expose the quality selector. That's the visible proof the adaptive part works.
- **The wrinkle:** Library and Admin create and destroy `<video>` elements on toggle, and both Plyr and hls.js leak unless explicitly `.destroy()`ed. Thread it through the existing `toggleInlinePlayer` helper — one place, not four.

### 7.2 The editing panel (~1.5 days)

The shell that hosts everything: the five operations as collapsible
sections, **downscale / upscale / convert** as plain dropdowns (downscale
and upscale mutually exclusive, so the UI can't build a request the worker
will reject), and mount points for B's crop box and C's scrubber. A
**Process** button that posts §1's body and then polls exactly like the
upload flow already does.

Build against a stub. The contract in §1 is fully specified, so nothing here
waits on A, B or C being finished.

- [ ] Every video in the app plays through Plyr — Upload, Library, Admin, the edit panel.
- [ ] A job with `hls_key` plays adaptively with a working quality selector; one without plays the MP4.
- [ ] No leaked player instances after ten Library toggles — check the memory profiler, not "it looks fine".
- [ ] Process cannot submit downscale and upscale together.

---

## 8. Not this sprint

- **Real (AI / super-resolution) upscaling.** `upscale` here is a lanczos resize. Actual super-resolution is GPU inference with its own deployment shape — a separate project, not a variant of this one.
- **`.mkv` inline playback.** A remux is nearly free server-side, but browsers don't all play `.mkv` in a `<video>` element. Flag it in the UI and offer download instead of silently breaking the player.
- **Video metadata, visibility and discovery.** Cut from this sprint. Worth building eventually; not worth crowding out the two features above.
- **YouTube references, moderation, the admin dashboard.** Still unbuilt, still real scope, still later.

The proposal lists more than this sprint contains. That's a deliberate
choice, not an oversight — better to ship two features people can see than
five nobody finishes.

---

## 9. Rules that have not changed

1. **`main` requires a pull request** and a review from another track.
2. **Ask before editing a file another track owns**, and say so in the PR when you do.
3. **Every file under `tests/integration/` needs the `RUN_POSTGRES_TESTS` guard.** An ungated one breaks CI and everyone's local run.
4. **The worker is the sole writer** to `status`, `output_key`, `error` and `updated_at` after the API's insert. All writes go through `app/repositories/jobs.py`, never raw SQL.
5. **Never commit credentials.** Check `git diff --cached --name-only` first.
6. **Deploy with both flags** — `docker compose up -d --build --scale worker=2`. Without `--build` your code silently doesn't ship (T-21); without `--scale` the second worker silently disappears (T-23).
