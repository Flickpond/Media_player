# Sprint 3 — Plan

**Written:** 11 September 2026 · against `main` @ `9922ad0`
**Sprint 2 ended:** 11 September 2026, all 8 items shipped and deployed.

Five tracks, one person per track, same shape as sprint 1: each track is a
**vertical slice** — its own backend, its own frontend where it needs one,
its own migration where it needs one — not a horizontal split by file type.
Sprint 2's real lesson (`sprint2-report.md` §2) was that cross-track work is
where friction happens; the fix here is fewer forced handoffs, not more
process. Read `docs/video-editing-plan.md` and `docs/known-traps.md` before
starting anything — the design work for Track A is already fully done.

---

## 0. The one thing every track needs to know before writing code

**Three tracks touch the `jobs` table's schema this sprint** (A adds
`operations`, B adds metadata + visibility columns, C adds `view_count`).
Alembic migrations are a straight line, not a tree — two people branching a
migration off the same starting revision at the same time will conflict the
moment both try to merge, because each one's `down_revision` points at the
same parent. **Track C merges its schema change first**, since C already
owns `migrations/`. Tracks A and B rebase their own migration on top of
whatever C lands, not in parallel from `main` as it stands today. Post in
the team channel before opening a migration PR so this doesn't turn into a
Thursday-night rebase.

Everything else follows the sprint 1 rule: ask before touching a file
outside your own track, and say so in the PR when you do.

---

## 1. Track A (this user) — the video editing suite's backend

**This is the sprint's core technical work — the piece everything else in
the feature depends on, and the one item here that's already fully
designed.** Read `docs/video-editing-plan.md` in full before starting;
everything below is a summary of a document that already answers most of
the hard questions.

### What ships

```
ALTER TABLE jobs ADD COLUMN operations JSONB;

POST /jobs/{id}/edit
  body: { "operations": [ { "operation": "crop", "params": {...} }, ... ] }
  202 { "job_id": "<uuid>" }
  404 { "error": "not found" }
  422 { "detail": [...] }   -- unknown operation, bad params, or both
                                downscale and upscale requested together
```

One new column, one new endpoint. No draft/library distinction, no undo
chain — the design's revision history in `video-editing-plan.md` explains
why that was the wrong shape for these five operations specifically.

### The worker: a fixed pipeline, not a user-configurable order

```
clip  ->  crop  ->  scale (downscale or upscale)  ->  convert
```

Not arbitrary. Clip's `-ss`/`-to` are input-side flags, so they already
bound every later step's frame count regardless of anything else. Crop's
`x`/`y`/`w`/`h` are anchored to the *original* frame — that's the only
correct reading of what the frontend's crop box draws, and it's also the
cheaper order, since it shrinks the pixel count before the expensive scale
+ encode work runs. `downscale` and `upscale` together is a 422, not a
guess. `convert` always runs last, since it changes the container/codec,
not the frame content.

| `operation` | `params` | FFmpeg shape | Needs `ffprobe` first? |
|---|---|---|---|
| `clip` | `{"start": 5.0, "end": 12.5}` | `-ss {start} -to {end}` | Yes — validate against source duration |
| `crop` | `{"x", "y", "w", "h"}` | `crop={w}:{h}:{x}:{y}` | Yes — validate rect fits inside source dimensions |
| `downscale` | `{"height": 480}` | `scale=-2:{height}` | No |
| `upscale` | `{"height": 1080}` | `scale=-2:{height}:flags=lanczos` | No |
| `convert` | `{"format": "mkv"}` / `{"format": "mp3"}` | remux (`-c copy`) or `-vn -c:a libmp3lame` | No |

`ffprobe` is a new step, not a new dependency — it ships in the same
package as `ffmpeg` on the worker image. `get_processing_step()` grows from
one hardcoded `FfmpegProcessor` into a registry (`{"crop": build_crop_args,
...}`) that assembles the fixed sequence above from whichever operations a
request actually included.

### Acceptance criteria

- [ ] `operations` column added, migration rebased on Track C's (§0).
- [ ] `POST /jobs/{id}/edit` accepts any non-empty subset of the five
      operations, in any order the client sends them, and always executes
      them in the fixed pipeline order above regardless.
- [ ] `downscale` + `upscale` together returns 422, not a silent pick of one.
- [ ] Crop and clip params are validated against real `ffprobe` output —
      a rect outside the frame or a range past the source's duration is
      422, not a crash mid-encode.
- [ ] A job with all five operations selected produces one output through
      one FFmpeg invocation — confirmed by checking the worker only shells
      out to `ffmpeg` once per edit job, not once per operation.
- [ ] Existing upload → transcode → play flow is untouched and its test
      suite still passes unmodified.

### Effort

~3.75 days foundation (schema + endpoint + worker registry, per
`video-editing-plan.md` §7) + ~1.5–2 days across downscale/upscale/convert
(the cheap three) + clip. **Crop's UI is Track E's, not this track's** —
see §5. Call it **5–6 days** for everything in this track's own lane.

---

## 2. Track B — video metadata and visibility

**Proposal Must Have** (§4.2), untouched since sprint 1. Right now a job has
a `filename` and nothing else a human chose — no title, no description, no
way to keep something private.

### What ships

```sql
ALTER TABLE jobs ADD COLUMN title Text;
ALTER TABLE jobs ADD COLUMN description Text;
ALTER TABLE jobs ADD COLUMN visibility Text NOT NULL DEFAULT 'private';
ALTER TABLE jobs ADD CONSTRAINT ck_jobs_visibility
  CHECK (visibility IN ('private', 'unlisted', 'public'));
```

- `POST /upload` accepts optional `title` — falls back to the filename if
  omitted, the same way a real upload form should never *require* one.
- `PATCH /jobs/{id}` — title, description, visibility. Owner-only, same
  404-not-403 pattern every other owner-scoped endpoint in this codebase
  already uses.
- `GET /jobs/{id}` gains `title`, `description`, `visibility` in its
  response — `response_model_exclude_none=True` already handles the
  optional fields correctly, nothing new needed there.
- Frontend: a small edit panel on a Library card (title/description fields,
  a three-way visibility toggle) — not a new view, an addition to the
  existing card.

**Coordinates with Track C**: `visibility = 'public'` is the flag Track C's
discovery page filters on. Land this schema change first if the two of you
can sequence it that way — makes C's work strictly additive rather than
something that has to wait on a column that doesn't exist yet.

### Acceptance criteria

- [ ] A job's title defaults to its filename when none is given at upload.
- [ ] `PATCH /jobs/{id}` on another owner's job is 404, matching every
      other owner-scoped route.
- [ ] `visibility` rejects anything outside the three allowed values at
      the database level, not just in a Pydantic schema — same "the
      database is the last line of defence" posture as `role`/`status`.
- [ ] Existing `GET /jobs`/`GET /jobs/{id}` responses are unchanged for a
      job created before this migration (title falls back to filename,
      visibility defaults to `private` — nobody's existing video becomes
      accidentally public).

### Effort

~3–4 days: migration + upload/patch endpoints + validation (~1.5 days),
frontend edit panel (~1.5–2 days).

---

## 3. Track C — discovery: browse, search, and view counts

**Proposal Must/Should Have** (§4.5). Right now "Library" is the only way
to see any video, and it only shows your own. There's no way for anyone to
find a video someone else made public.

### What ships

```sql
ALTER TABLE jobs ADD COLUMN view_count BIGINT NOT NULL DEFAULT 0;
```

- `GET /discover?q=<text>&limit=&offset=` — returns `visibility = 'public'`
  jobs only, across every owner, with a simple `ILIKE` match against title
  and filename. Not full-text search — that's real added infrastructure
  this sprint doesn't need to take on for a handful of public videos.
- `POST /jobs/{id}/view` — increments `view_count` by one. Called once by
  the player when playback starts, not on every poll. No auth requirement
  beyond "the video is public or you own it" — a view on someone else's
  public video is exactly the point.
- Frontend: a new "Discover" tab, public to any signed-in user, a grid
  matching Library's existing card layout with a view count shown per card.

**Depends on Track B's `visibility` column existing** — there is nothing to
discover until a video can be public. Coordinate the two migrations per §0
rather than each guessing where the other will land.

### Acceptance criteria

- [ ] `GET /discover` never returns a `private` or `unlisted` job, under
      any query — this is the one query in the whole feature that
      absolutely cannot leak, the same class of mistake P1 was.
- [ ] Search matches are case-insensitive and match partial words.
- [ ] `view_count` increments are idempotent-enough that refreshing the
      page mid-playback doesn't matter — one increment per playback start,
      not per poll tick.
- [ ] Pagination follows the same `limit`/`offset` convention as
      `GET /jobs`, not a new one.

### Effort

~4–5 days: schema + `/discover` + `/view` + query scoping (~2 days),
frontend Discover tab (~2–3 days).

---

## 4. Track D — the DevSecOps gap: SAST, DAST, load testing, IaC

**Proposal §5.4 and §6, fully unbuilt since sprint 1.** This is the
starkest gap between the proposal and what actually exists: the CI pipeline
scans container images (Trivy) but has no SAST, no DAST, and nobody has
ever run the 50-concurrent-user load test the proposal's own performance
requirement (§5.1) calls for. This track is DevOps-flavoured, which is
squarely where Track D's existing ownership (`docker-compose.yml`,
`nginx.conf`, CI-adjacent infra) already sits — no reason to hand this to
someone starting cold on it.

### What ships

- **SAST**: a SonarQube (or, if a hosted SonarQube account isn't practical
  for a student project, `bandit` + `ruff`'s security-relevant rules as a
  documented, honest substitute — say which was actually used and why in
  the sprint report, don't claim SonarQube if it wasn't) scan added as a
  CI job, gating on zero critical findings, the same way Trivy already
  gates on the image scan.
- **DAST**: an OWASP ZAP baseline scan against a running instance of the
  stack in CI (spin up `docker compose`, point ZAP at it, fail the job on
  high-severity findings) — the proposal's own wording, §5.4.
- **Load test**: a real, recorded run against the actual deployment —
  50 concurrent simulated users (`locust` or `k6`, either is fine, pick
  whichever the person on this track already knows), hitting the
  non-video endpoints, checking the proposal's actual number: 90% under
  2 seconds (§5.1). Publish the real numbers in `docs/scaling-notes.md`,
  replacing its current analysis-only content with actual measured
  evidence.
- **IaC**: a Terraform config that reproduces the current hand-built
  Alibaba ECS deployment — the instance, the security group, the DNS
  records. Doesn't need to *replace* the current SSH-and-`docker-compose`
  deploy flow this sprint; documenting the infrastructure as code and
  proving `terraform plan` matches what's actually running is the real
  deliverable, migrating the live deploy to `terraform apply` is a
  reasonable sprint 4 follow-up if this goes well.

### Acceptance criteria

- [ ] SAST and DAST both run as required CI checks, joining the existing
      five, with real (not placeholder) rules — "the step exists and
      always passes" isn't the goal.
- [ ] The load test's actual numbers are recorded in `scaling-notes.md`
      with the command used to produce them, so anyone can rerun it.
- [ ] `terraform plan` against the real deployment shows no unexpected
      drift for the resources it declares.

### Effort

~5–6 days — genuinely the least-scoped item here because none of it exists
yet to build on top of; expect more discovery time than the other tracks.

---

## 5. Track E — frontend: the editing UI, and the player upgrade

Two pieces, both frontend-only, both able to start immediately without
waiting on Track A.

### The player, first — ships fast, ships alone

`docs/video-editing-plan.md` §9: native `<video controls>` is used
everywhere in the app today and looks dated, inconsistent across browsers,
with no speed or scrub controls. Swap it for **Plyr** — wraps the existing
`<video>` element, no bundler needed, themes through the CSS custom
properties `style.css` already defines. The one real wrinkle: Library and
Admin create/destroy `<video>` elements dynamically on toggle, and a Plyr
instance needs an explicit `.destroy()` when its element is removed, or it
leaks — thread that through the existing `toggleInlinePlayer` helper, not
four separate places. **~1 day.** Good first thing to ship this sprint —
real, visible, and doesn't block on anyone else.

### The editing view — the UI for Track A's endpoint

The crop box is the one genuinely new interactive component in this whole
sprint, and it's substantial enough to be its own person's main work rather
than an add-on to Track A's backend:

- A panel below the player listing the five operations, each
  collapsible/optional.
- Crop: a draggable/resizable rectangle over a paused video frame,
  converting on-screen pixels to the source's real pixel coordinates.
  Worth a small library (cropperjs) over hand-rolled drag math.
- Clip: a trim range on a scrubber, previewed against the already-loaded
  original — no server round trip to see what a clip will produce.
- Downscale/Upscale/Convert: plain dropdowns, mutually exclusive on
  downscale/upscale so the UI never lets someone build a request the
  worker will reject.
- **Process** calls `POST /jobs/{id}/edit`, then polls exactly like the
  upload flow already does.

**Doesn't have to wait on Track A being finished.** Build the panel, the
crop box, and the clip scrubber against a stubbed response first — the
contract in §1 is already fully specified, so there's nothing to guess at —
and wire up the real endpoint once Track A's is live. This is the normal
shape of frontend/backend parallel work and shouldn't be a bottleneck for
either side if both start from the same documented contract.

### Acceptance criteria

- [ ] Every place a video plays (Upload, Library, Admin, the new edit view)
      uses Plyr, not a bare `<video controls>`.
- [ ] No leaked player instances after ten toggles of Library's
      click-to-expand (check via the browser's memory profiler, not just
      "it looks fine").
- [ ] The crop box's reported rectangle, converted back to source pixels,
      matches what a human would expect from where they dragged it —
      test this against a source with a *non-square, non-16:9* aspect
      ratio, where a naive pixel-to-pixel mapping bug is most likely to
      show up.
- [ ] Clip's scrubber never lets `start >= end`.

### Effort

~1 day (player) + ~4–5 days (editing view, crop UI included) = **~5–6
days.**

---

## 6. What's deliberately not in this sprint

YouTube external references, moderation/reporting, and the admin summary
dashboard are all real proposal scope, all still unbuilt, and all left out
here on purpose — five tracks, and this sprint already asks each one for a
genuine, complete, independently-shippable feature. Better to finish five
things than start eight. Good candidates for sprint 4, in roughly that
priority order.
