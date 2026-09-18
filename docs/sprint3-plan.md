# Sprint 3 — Plan

**Window:** Friday 18 September – Friday 25 September 2026
**Sprint 2 ended:** 11 September 2026, all 8 items shipped and deployed.
**Written against:** `main` @ `9922ad0`

**Sprint goal:** adaptive playback and a retryable pipeline — the two
processing Must Haves from the proposal that are still unbuilt — plus
discovery, video metadata, and the first half of the editing suite.

Five tracks, one person per track, same shape as sprints 1 and 2: each track
is a **vertical slice** — its own backend, its own frontend where it needs
one — not a horizontal split by file type. Sprint 2's lesson
(`sprint2-report.md` §2) was that cross-track work is where friction happens;
the fix is fewer forced handoffs, not more process.

**Read before writing any code:** [`known-traps.md`](known-traps.md) (24
traps already hit on this project — most fail silently),
[`contract.md`](contract.md) (the shared API boundary), and your own track's
section below.

---

## 0. Who has what

| Track | Owner | This sprint's deliverable | Effort |
|---|---|---|--:|
| **A** | Ibrahim Mammadov (@1brahim74) | HLS adaptive playback (worker + delivery), job retry, editing-suite backend | ~5.5 d |
| **B** | Zhang Jizhang (@zhanj384) | Video metadata and visibility (title, description, private/unlisted/public) | ~3.5 d |
| **C** | Yang Dongwei (@ttydw-ch) | The day-1 migration, then discovery: browse, search, view counts | ~4.5 d |
| **D** | Jiang Yibai (@JiangYibai666) | DevSecOps: SAST, DAST, the 50-user load test, Terraform | ~5 d |
| **E** | Lu Jingxing (@Dilute-l) | The player: Plyr + hls.js adaptive playback, and the editing controls | ~4 d |

File ownership carries over from sprint 1 (see `CLAUDE.md`). **Ask in the
group before editing a file another track owns**, and say so in the PR when
you do.

### Why this scope

The proposal's own sprint 3 row asks for five things. Three of them are in
this plan: **retry** and **adaptive HLS playback** (both Must Haves, both
still unbuilt after two sprints) and **browse/search/view counts**. Two are
deferred on the record with a reason — see §9. The editing suite is not in
the proposal at all; it ships here because its design is already done
(`video-editing-plan.md`) and because it is what demonstrates the Strategy
pattern the proposal also asks us to document (§8).

---

## 1. The one blocking event: Friday's migration

**Three tracks need new columns on `jobs` this sprint.** Alembic migrations
are a straight line, not a tree: two people branching a migration off the
same revision both end up with `down_revision` pointing at the same parent,
and they conflict the moment the second one merges. In a one-week sprint
there is no room for that.

**So there is exactly one migration this sprint, it contains every track's
columns, and Track C writes and merges it on day 1** — C already owns
`migrations/`. Everyone else writes model and endpoint code against columns
that already exist.

```sql
-- One migration, merged Friday 18 September. Track C authors it.
ALTER TABLE jobs ADD COLUMN operations  JSONB;                              -- Track A
ALTER TABLE jobs ADD COLUMN hls_key     TEXT;                               -- Track A
ALTER TABLE jobs ADD COLUMN title       TEXT;                               -- Track B
ALTER TABLE jobs ADD COLUMN description TEXT;                               -- Track B
ALTER TABLE jobs ADD COLUMN visibility  TEXT NOT NULL DEFAULT 'private';    -- Track B
ALTER TABLE jobs ADD COLUMN view_count  BIGINT NOT NULL DEFAULT 0;          -- Track C

ALTER TABLE jobs ADD CONSTRAINT ck_jobs_visibility
  CHECK (visibility IN ('private', 'unlisted', 'public'));
ALTER TABLE jobs ADD CONSTRAINT ck_jobs_operations_nonempty_array
  CHECK (operations IS NULL OR
         (jsonb_typeof(operations) = 'array' AND jsonb_array_length(operations) > 0));
```

Every column is nullable or has a default, so no existing row needs a data
migration and **no existing video becomes accidentally public** —
`visibility` defaults to `private`.

Post in the group the moment it is merged. Nobody else opens a migration
this sprint; if you think you need one, say so in the group first.

---

## 2. Schedule

| When | What |
|---|---|
| **Fri 18 Sep** | Plan out. Track C writes and merges the migration (§1). Everyone else reads their section and `known-traps.md`. |
| **Sat 19 – Sun 20 Sep** | Build. Track A's HLS ladder producing files locally by end of Sunday. |
| **Mon 21 Sep** | Every track posts a one-line status in the group: what's merged, what's blocked. |
| **Tue 22 – Wed 23 Sep** | Build. **Wed 23 is feature freeze** — everything you intend to ship is in an open PR by end of day. |
| **Thu 24 Sep** | Review and merge. Deploy to flickpond.com and verify there, not just locally. |
| **Fri 25 Sep** | Sprint 3 report, design-patterns doc (§8), retrospective. |

The deploy on Thursday is the real deadline. A PR that merges Friday morning
did not ship this sprint.

---

## 3. Track A — adaptive playback, retry, and the editing backend

**Owner: Ibrahim (@1brahim74).** Three items, in priority order. The first
two are proposal Must Haves and come first; the third is the one that slips
if the week compresses.

### 3.1 HLS adaptive playback — the sprint's headline (~2.5 days)

The proposal's overview promises videos "processed asynchronously into
adaptive HLS formats." Today the worker makes one 720p MP4. This adds a
three-rendition HLS ladder **alongside** that MP4 — additive, not a
replacement, so if HLS generation fails the existing playback and download
path is untouched.

**Worker.** One extra FFmpeg invocation after the existing transcode,
producing 360p / 480p / 720p variants and a master playlist:

```
outputs/{job_id}/hls/master.m3u8      <- hls_key points here
outputs/{job_id}/hls/v0/seg00001.ts   <- 360p segments
outputs/{job_id}/hls/v1/...           <- 480p
outputs/{job_id}/hls/v2/...           <- 720p
```

Built with `-var_stream_map` and `-master_pl_name`, capped so a 480p source
never gets upscaled into a 720p rendition it has no detail for. Every object
is uploaded under the one prefix, and `hls_key` on the job row points at the
master playlist. `output_key` keeps pointing at the MP4, unchanged.

**Delivery — the part that is actually hard.** Every output object today is
reached through a presigned MinIO URL. An HLS ladder is hundreds of objects,
and a player cannot ask us to sign each one in advance. Making the prefix
public-read would solve it and is **not acceptable** — that is exactly the
P1 class of mistake sprint 1 spent its review fixing.

The answer is a new route, `GET /api/jobs/{id}/hls/{path}`, which checks the
caller owns the job (or it is public, once Track B lands visibility), then
**307-redirects to a freshly presigned URL** for that one object. FFmpeg
writes playlists with relative segment names, so the player resolves them
against the route path and the whole ladder works with no playlist rewriting
at all. New file, `app/api/hls.py` — no other track's file changes.

- [ ] A processed video has an `hls_key` and a master playlist that lists three variants.
- [ ] A 480p source produces at most a 480p top rendition, never an upscaled one.
- [ ] `GET /api/jobs/{id}/hls/master.m3u8` on another owner's private job is 404, same as every other owner-scoped route.
- [ ] MP4 playback and download still work exactly as before for every existing job.

### 3.2 Retry (~0.75 day)

Proposal Must Have ("asynchronous processing with status and retry") and
§5.2: "Failed processing jobs must be retryable, and repeating the same job
must not create conflicting video records or duplicate published outputs."

`POST /jobs/{id}/retry` — a conditional `UPDATE ... WHERE id = ? AND status
= 'failed'` back to `queued`, clearing `error` and `output_key`, then
re-enqueue. **No schema change**, which is why it is not in §1's migration.

Two concurrent retries cannot both win: the conditional update is the same
race-safe pattern `mark_processing` already uses, so one gets the row and the
other gets a 409. Duplicate outputs are impossible because the output key is
derived from the job id, so a retry overwrites rather than accumulating.

This does relax the "transitions are one-way" rule in `CLAUDE.md` — say so
in the PR, and update that rule's wording. The safety it actually provides
comes from the conditional update, not from the one-way property, and
`failed -> queued` is still the only backwards edge.

- [ ] Retrying a `failed` job re-runs it and can reach `done`.
- [ ] Retrying a `queued`, `processing` or `done` job is 409, not a silent no-op.
- [ ] Two retries fired at once produce one queue entry, not two.

### 3.3 Editing suite — foundation and the three cheap operations (~2.25 days)

Per `video-editing-plan.md`: the `operations` column (already in §1's
migration), `POST /jobs/{id}/edit`, the worker's fixed
clip→crop→scale→convert pipeline as a registry, and the three operations
that are a dropdown and a registry entry each — **downscale, upscale,
convert**.

**Crop and clip are deferred to sprint 4** (§9). They are the two that need
`ffprobe` validation on the backend *and* a custom interactive component on
the frontend, and neither half fits a one-week sprint honestly.

- [ ] `POST /jobs/{id}/edit` accepts any non-empty subset of the three shipped operations and runs them in the fixed pipeline order regardless of request order.
- [ ] `downscale` + `upscale` together is 422, not a silent pick of one.
- [ ] A job with all three operations shells out to `ffmpeg` **once**, not once per operation.
- [ ] The existing upload → transcode → play flow and its tests are untouched.

**If the week compresses, 3.3 is what slips** — not 3.1 or 3.2. Say so in
Monday's status rather than at the freeze.

---

## 4. Track B — video metadata and visibility

**Owner: Zhang Jizhang (@zhanj384).** Proposal Must Have (§4.2), untouched
since sprint 1. A job today has a `filename` and nothing a human chose — no
title, no description, no way to keep something private.

Columns land in §1's migration, so this track starts at the model and
endpoint layer with nothing to wait for.

- `POST /upload` accepts an optional `title`, falling back to the filename
  when it is omitted — a real upload form should never *require* one.
- `PATCH /jobs/{id}` — title, description, visibility. Owner-only, and
  another owner's job is **404, never 403**, matching every other
  owner-scoped route in this codebase. A 403 confirms the id exists.
- `GET /jobs/{id}` and `GET /jobs` gain the three fields.
  `response_model_exclude_none=True` already handles the optional ones.
- Frontend: a small edit panel on a Library card — title and description
  fields, a three-way visibility toggle. An addition to the existing card,
  not a new view.

**Track C's discovery page filters on `visibility = 'public'`**, so this is
the track that makes C's feature mean anything. If you finish early, help C.

- [ ] Title defaults to the filename when none is given at upload.
- [ ] `PATCH /jobs/{id}` on someone else's job is 404.
- [ ] `visibility` rejects anything outside the three values **at the database level**, not only in a Pydantic schema — same posture as `role` and `status`.
- [ ] Every job created before this sprint reads back as `private` with its filename as the title. Nobody's existing video becomes public.

---

## 5. Track C — the migration, then discovery

**Owner: Yang Dongwei (@ttydw-ch).** Proposal Must/Should Have (§4.5).
"Library" only shows your own videos; there is no way to find anyone else's.

**Day 1 is §1's migration.** Everyone else is waiting on it, so it goes
first and it goes in alone — no feature code in that PR.

Then:

- `GET /discover?q=&limit=&offset=` — `visibility = 'public'` jobs only,
  across every owner, with a case-insensitive `ILIKE` match on title and
  filename. Not full-text search; that is real infrastructure this sprint
  does not need for a handful of public videos.
- `POST /jobs/{id}/view` — increments `view_count`. Called once when
  playback starts, not on every poll tick.
- Frontend: a "Discover" tab, matching Library's existing card layout, with
  a view count per card.

- [ ] `GET /discover` never returns a `private` or `unlisted` job under **any** query. This is the one query in the sprint that absolutely cannot leak — same class of mistake as P1.
- [ ] Search is case-insensitive and matches partial words.
- [ ] One increment per playback start, not per poll.
- [ ] Pagination uses the same `limit`/`offset` convention as `GET /jobs`, not a new one.

---

## 6. Track D — DevSecOps: SAST, DAST, load test, IaC

**Owner: Jiang Yibai (@JiangYibai666).** Proposal §5.4 and §6, fully unbuilt
since sprint 1 — the starkest gap between the proposal and what exists. CI
scans container images (Trivy) but there is no SAST, no DAST, and nobody has
run the 50-concurrent-user load test the proposal's own performance
requirement (§5.1) calls for.

This track blocks on nobody and nobody blocks on it, which makes it the
safest place for the week to go sideways.

- **SAST** — a SonarQube scan as a CI job, gating on zero critical findings
  the way Trivy already gates. If a hosted SonarQube account is not
  practical for a student project, `bandit` plus ruff's security rules is an
  honest substitute — **say which you actually used and why** in the sprint
  report. Do not claim SonarQube if it was not SonarQube.
- **DAST** — an OWASP ZAP baseline scan against the stack running in CI
  (`docker compose up`, point ZAP at it, fail on high-severity findings).
- **Load test** — a real recorded run against the deployment: 50 concurrent
  users (`locust` or `k6`, whichever you know) against the non-video
  endpoints, checking the proposal's actual number — 90% under 2 seconds
  (§5.1). Put the real numbers in `scaling-notes.md`, replacing its
  analysis-only content with measured evidence.
- **IaC** — Terraform describing the current hand-built Alibaba ECS
  deployment: the instance, the security group, the DNS records. It does not
  have to replace the SSH-and-compose deploy this sprint; `terraform plan`
  matching what is actually running is the deliverable.

**One dependency to watch:** Track A's HLS route serves many small files per
video. If the load test or the deploy shows nginx needs tuning for that,
that is your file (`deploy/nginx/locations.conf`) — coordinate with A.

- [ ] SAST and DAST both run as required CI checks with real rules. "The step exists and always passes" is not the goal.
- [ ] The load test's numbers are in `scaling-notes.md` with the command used, so anyone can re-run it.
- [ ] `terraform plan` shows no unexpected drift for the resources it declares.

---

## 7. Track E — the player: adaptive playback and the editing controls

**Owner: Lu Jingxing (@Dilute-l).** Two pieces. The first is what makes
Track A's HLS work visible to a user, and it is the one the proposal
actually asks for.

### 7.1 Plyr + hls.js — adaptive playback (~2.5 days)

Every place a video plays today — the Upload page's active-job card,
Library's click-to-expand, Admin's Watch toggle — uses a bare `<video
controls>`: dated, inconsistent across browsers, no speed control, no
quality selector.

Replace it with **Plyr**, which wraps the existing `<video>` element rather
than replacing it, needs no bundler (this project has none and is not
getting one), and themes through the CSS custom properties `style.css`
already defines. Then wire **hls.js** into it: Safari plays HLS natively,
Chrome and Firefox do not.

- Play `hls_key` through hls.js when the job has one; **fall back to the MP4
  `output_url` when it does not.** Every job created before this sprint has
  no `hls_key`, and they must all still play.
- Expose the quality selector Plyr gets from the hls.js integration — that
  is the visible proof of "adaptive playback" for the demo.
- **The one real wrinkle:** Library and Admin create and destroy `<video>`
  elements on toggle, and both a Plyr instance and an hls.js instance need
  an explicit `.destroy()` when their element goes away, or they leak. Thread
  that through the existing `toggleInlinePlayer` helper — one place, not four.

### 7.2 Editing controls (~1.5 days)

The UI for Track A's `POST /jobs/{id}/edit`: a panel below the player with
**downscale, upscale and convert** as plain dropdowns, downscale and upscale
mutually exclusive so the UI never builds a request the worker will reject,
and a **Process** button that calls the endpoint and then polls exactly like
the upload flow already does.

No crop box and no clip scrubber this sprint — those defer with A's half of
the same feature (§9).

**You do not have to wait for Track A.** The contract in §3.3 is fully
specified; build against a stub and wire up the real endpoint when it lands.

- [ ] Every place a video plays uses Plyr — Upload, Library, Admin, the edit panel.
- [ ] A job with `hls_key` plays adaptively with a working quality selector; a job without one plays the MP4.
- [ ] No leaked player instances after ten toggles of Library's click-to-expand — check the memory profiler, not "it looks fine".
- [ ] The Process button cannot submit downscale and upscale together.

---

## 8. Design patterns — everyone writes one section

The proposal's sprint 3 row asks us to "apply **and document** design
patterns (State, Strategy, Adapter, Observer)." This is a graded
deliverable for a *Designing Modern Software Systems* certificate and it is
currently documented nowhere.

The good news: all four already exist in this codebase. This is mostly
writing down what is there.

`docs/design-patterns.md`, one section each, **due Friday 25 Sep**:

| Pattern | Where it already lives | Who writes it |
|---|---|---|
| **State** | The job lifecycle — `queued → processing → done \| failed`, plus the new `failed → queued` retry edge, enforced by conditional updates in `repositories/jobs.py` | A |
| **Strategy** | `ProcessingStep` — one interface, swappable implementations (`CopyProcessor`, `FfmpegProcessor`, and the new operation registry) | A |
| **Adapter** | `MinioObjectStore` wrapping the MinIO SDK behind the `ObjectStore` protocol, so the worker never imports MinIO directly | B |
| **Observer** | The frontend's 2-second polling and the RQ queue as two different takes on "tell me when something changed" | E |

Track C adds a short section on the repository pattern behind
`repositories/jobs.py`; Track D adds one on pipeline-as-code. About an hour
each. Write *why the pattern is there*, not a textbook definition — the
interesting part is that `ProcessingStep` is what let sprint 2 swap a file
copy for FFmpeg without touching the state machine.

---

## 9. Deferred to sprint 4, on the record

Not oversights. Each is a decision with a reason and a cost:

- **Crop and clip.** The two editing operations needing `ffprobe` geometry
  validation on the backend *and* a custom interactive component
  (draggable crop box, trim scrubber) on the frontend. ~4 days across two
  tracks. Deferred together so the feature stays coherent rather than
  shipping a backend with no UI.
- **YouTube references** (proposal Should Have). An external API integration
  with its own failure modes and its own Adapter to write. Real scope; it
  needs a week where it is somebody's main item, not a corner of one.
- **Moderation and reporting**, and the **admin summary dashboard** — both
  proposal sprint 4 items already, left where the proposal puts them.
- **Resumable upload with progress** (proposal sprint 2, never built). Worth
  naming so it does not quietly disappear from the record.

Sprint 4 is already the heaviest sprint in the proposal — moderation, the
admin dashboard, final QA, the demo. Going in with this list written down
beats discovering it in the last week.

---

## 10. Rules that have not changed

1. **`main` requires a pull request** and a review from another track.
2. **Ask before editing a file another track owns.** Say so in the PR when you do.
3. **Every file under `tests/integration/` needs the `RUN_POSTGRES_TESTS` guard.** An ungated one breaks CI and everyone's local test run.
4. **The worker is the sole writer** to `status`, `output_key`, `error` and `updated_at` after the API's insert. All writes go through `app/repositories/jobs.py`, never raw SQL.
5. **Never commit credentials.** Check `git diff --cached --name-only` before committing.
6. **Deploy with both flags** — `docker compose up -d --build --scale worker=2`. Without `--build` your code silently does not ship (T-21); without `--scale` the second worker silently disappears (T-23).
