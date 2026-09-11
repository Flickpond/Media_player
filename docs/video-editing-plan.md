# Video editing suite — design and plan

**Written:** 11 September 2026 · against `main` @ `9922ad0`
**Status:** design only. Nothing in this document is built yet.

This is not part of the original proposal — crop/clip/scale/convert tools
aren't in `proposal.md`'s scope at all. Where it lands (sprint 3, replacing
some of the proposal's original sprint 3 content, or a new sprint 5) is a
team call; this document specifies *what* and *how*, not *when*.

Read [`known-traps.md`](known-traps.md) and [`contract.md`](contract.md)
before touching any of this — the same rule as every other plan in this repo.

---

## 1. The exact flow this is built for

Stated here in full because it's the spec, not a summary of it:

1. A video finishes uploading and processing (the existing pipeline,
   unchanged). It's playable.
2. The user presses an operation button (Crop, Downscale, Upscale, Clip,
   Convert) below the player, supplies whatever that operation needs, and
   presses **Save**. The result is **not** added to the library yet — it's a
   *working draft*. Durable: if the user's connection drops for 20 seconds and
   they come back, the draft is still there. It rides on the existing job
   pipeline (Postgres + MinIO), so this is true for free — nothing ephemeral
   in the browser holds it.
3. The user presses another operation button. It operates on the **current
   draft**, not the original. They press Save again.
4. The moment the new draft finishes successfully, **the previous draft is
   deleted** — both its database row and its storage objects. There is at
   most one draft in existence per original video, at all times.
5. This repeats for as many operations as the user wants.
6. At any point, the user can discard the current draft and go back to the
   untouched original — which was never modified by any of this.
7. When satisfied, the user presses **Save to Library**. The current draft
   becomes a real library entry — downloadable, listed, playable like any
   other video. The original stays in the library too, separately, so a
   later editing session can start fresh from it.

One draft slot per original, replaced (never accumulated) on every successful
step, promoted to a permanent entry only on explicit confirmation. Everything
below exists to make that sequence correct under real conditions — a step
that fails, a browser closed mid-edit, two tabs open on the same video.

---

## 2. Data model

Extends `jobs` rather than introducing a parallel table — an edit produces
exactly the same shape of thing an upload does (a source object, a job, an
output object), so it reuses the same state machine, the same worker, the
same reaper.

```sql
ALTER TABLE jobs ADD COLUMN kind Text NOT NULL DEFAULT 'upload';
ALTER TABLE jobs ADD COLUMN parent_job_id UUID REFERENCES jobs(id);
ALTER TABLE jobs ADD COLUMN origin_job_id UUID REFERENCES jobs(id);
ALTER TABLE jobs ADD COLUMN operation Text;
ALTER TABLE jobs ADD COLUMN operation_params JSONB;

ALTER TABLE jobs ADD CONSTRAINT ck_jobs_kind
  CHECK (kind IN ('upload', 'draft', 'library'));

-- The load-bearing constraint: the database refuses a second draft for the
-- same original before the first is gone, so two tabs racing to save can't
-- both succeed and leave two drafts alive -- one of the two INSERTs just
-- fails. No application-level locking needed for that case.
CREATE UNIQUE INDEX ux_jobs_one_draft_per_origin
  ON jobs (origin_job_id) WHERE kind = 'draft';
```

- **`kind`** — `upload` (an original, exactly today's rows), `draft` (a
  working edit, invisible to `GET /jobs`), `library` (a promoted, permanent
  entry — same visibility as `upload`).
- **`parent_job_id`** — the immediate source: the job this one was derived
  from. A draft's parent can be the original or a previous draft.
- **`origin_job_id`** — the root original this whole chain traces back to.
  `NULL` on an `upload` row (it *is* the origin); always a concrete id on a
  `draft` or promoted `library` row. This is what the unique index enforces
  against, and what "find the current draft for this video" queries against
  directly, without walking `parent_job_id` chains.
- **`operation`** / **`operation_params`** — `NULL` on an upload.
  `operation_params` is a small JSON object, shape per operation (§5).

`GET /jobs` (the library) filters to `kind IN ('upload', 'library')` — a
real, necessary change to `list_jobs`'s existing query, since a `draft` row
must never appear there. **This is a contract change** — flag it in
`contract.md` when built.

---

## 3. New endpoints

```
POST /jobs/{id}/edit
  body: { "operation": "crop", "params": {...} }
  202 { "job_id": "<uuid>" }
  404 { "error": "not found" }        -- unknown id, or not this caller's
  422 { "detail": [...] }             -- operation unknown, or params fail
                                          that operation's validation

  `id` is whatever the user is currently looking at -- the original, or the
  current draft. The new row's parent_job_id = id; its origin_job_id is id's
  own origin_job_id if it has one, else id itself. Processed exactly like an
  upload (queued -> processing -> done | failed) by the same worker.

POST /jobs/{id}/promote
  204                                 -- id must be a `draft` this caller owns,
                                          status = done
  404 { "error": "not found" }
  409 { "error": "job is not a finished draft" }

  Flips kind: 'draft' -> 'library'. No new processing -- the object already
  exists, this only changes what GET /jobs returns.

GET /jobs/{id}
  -- extended, not replaced. When `id` is an origin (upload or library) with
  -- a live draft, the response gains one field:
  200 { ..., "draft"?: { "id", "status", "output_url"?, "error"? } }
```

**Discarding a draft needs no new endpoint** — `DELETE /jobs/{id}` (already
built) works on a `draft` row exactly as it works on anything else. "Go back
to the original" is just deleting the current draft; the original was never
touched.

---

## 4. Who deletes the old draft, and when

**Order matters, and it's the part of this whole feature most likely to be
built wrong under time pressure:** the old draft is deleted **after** the new
one reaches `done`, never before. If a step fails, the user still has their
last good draft to keep working from or fall back to — the alternative
(delete-then-create) would mean one failed operation loses their progress.

This happens in the worker, as one more write in the same place it already
owns the job's state transitions (N4): the instant a derive-job (a job with
`parent_job_id` set) is marked `done`, the worker looks up the sibling draft
for the same `origin_job_id` — the one this new job is replacing — and
deletes it, DB row and storage objects together, the same
delete-then-best-effort-storage-cleanup shape `DELETE /jobs/{id}` already
uses. A failure to delete the old draft's storage is the same class of
harmless leftover the reaper's sweep already cleans up; it must never block
the new draft from being usable.

---

## 5. Worker dispatch: from one fixed step to five, plus a probe

Today, `get_processing_step()` builds one `FfmpegProcessor` at startup,
running the same hardcoded command for every job. This needs to become a
per-job choice, dispatched on `job.operation`:

| `operation` | `operation_params` | FFmpeg shape | Needs `ffprobe` first? |
|---|---|---|---|
| `downscale` | `{"height": 480}` | `scale=-2:{height}` | No |
| `upscale` | `{"height": 1080}` | `scale=-2:{height}:flags=lanczos` | No |
| `clip` | `{"start": 5.0, "end": 12.5}` | `-ss {start} -to {end}` | Yes — validate against source duration |
| `crop` | `{"x": 0, "y": 140, "w": 1080, "h": 1080}` | `crop={w}:{h}:{x}:{y}` | Yes — validate rect fits inside source dimensions |
| `convert` | `{"format": "mkv"}` or `{"format": "mp3"}` | remux (`-c copy`) for mkv; `-vn -c:a libmp3lame` for mp3 | No |

`ffprobe` is a new dependency on the worker image (ships alongside `ffmpeg`
already, same package on Debian/Alpine — should be a one-line Dockerfile
change, not a new install path) and a new small step: read width/height/
duration from the source before building the command, for the two operations
whose parameters can otherwise describe something that doesn't exist (a crop
rect outside the frame, a clip range past the end of the video).

A registry — `{"downscale": build_downscale_args, "crop": build_crop_args,
...}` — replaces the single hardcoded command list. This is the same seam
`sprint2-plan.md` already left for exactly this ("a future processor
implements `ProcessingStep`") — it needs to grow from *one* processor
selected once at startup into *one selected per job*, not be fought against.

**Deliberately excluded: real (AI/super-resolution) upscaling.** The
`upscale` operation above is a plain filter upscale — same mechanism as
downscale, visibly softer than the source, no new dependency. Genuine quality
gain needs a model (e.g. Real-ESRGAN) and realistically a GPU worker; that's
a separate initiative with its own deployment shape, not a variant of this
one, and isn't included in the estimate below.

---

## 6. Frontend

**A new view, not buttons bolted onto two existing ones.** Both entry points
— the just-finished upload on the Upload page, and any video opened from
Library — need the same editing surface, so it's one shared component:

- The player, unchanged.
- A row of operation buttons below it (wraps on narrow screens rather than
  trying to hold left/right flanks at phone width).
- Pressing one opens that operation's input: a dropdown for
  downscale/upscale/convert, two number fields (or a scrubber, if time
  allows — see effort table) for clip, an interactive crop box for crop.
- **Save** — calls `POST /jobs/{id}/edit`, then polls exactly like the
  upload flow already does (same badge/progress-bar pattern, nothing new to
  build there) until the new draft is `done`.
- **Save to Library** — visible whenever a draft exists; calls
  `POST /jobs/{id}/promote`, then returns to Library.
- **Discard** / **back to original** — `DELETE` on the current draft.
- On opening a video that already has a live draft (`GET /jobs/{id}`'s new
  `draft` field), resume showing that draft, not the original — someone who
  left mid-edit and came back should see where they left off.

The crop box is the one genuinely new interactive component: a
draggable/resizable rectangle over a paused video frame, converting on-screen
pixels back to the source's real pixel coordinates, optionally locked to a
target aspect ratio. Worth a small library (e.g. cropperjs) rather than
hand-rolling drag math — everything else in this feature is a form.

---

## 7. What this deliberately does not solve

- **Two tabs editing the same video at once.** The unique index makes this
  *safe* (one save wins, the other's `POST /jobs/{id}/edit` fails rather than
  corrupting anything), not *smooth* — the losing tab needs to handle a 409
  gracefully rather than silently overwrite. Worth a plain error message,
  not a merge.
- **`.mkv` playback.** A remux is nearly free server-side, but some browsers
  don't play `.mkv` in a plain `<video>` element as well as `.mp4` — a
  converted-to-mkv result may need to fall back to "Download" rather than
  "Watch inline." Flag this at the UI, don't silently break the player.
- **Real upscaling.** See §5.

---

## 8. Effort

Same shared foundation as before, revised now that the draft lifecycle is
specified precisely rather than assumed:

| Piece | Estimate |
|---|---|
| Schema migration, `kind`/parent/origin columns, the unique-draft index | 0.5 day |
| `POST /jobs/{id}/edit` + `POST /jobs/{id}/promote`, `list_jobs` excluding drafts | 1 day |
| Worker: operation registry, `ffprobe` step, delete-old-draft-on-success | 1.5 days |
| Frontend: shared edit view, Save / Save to Library / Discard, resume-a-live-draft | 1.5 days |
| **Foundation subtotal** | **~4.5 days** |
| Downscale (dropdown + registry entry) | 0.5 day |
| Upscale, filter-based (dropdown + registry entry) | 0.5 day |
| Convert: mp4→mkv (registry entry; mkv playback caveat handling) | 0.5 day |
| Convert: mp4→mp3 (registry entry; `<audio>` render path for an audio result) | 1 day |
| Clip (duration validation; two number fields) | 1 day |
| Clip, with a real scrubber instead of number fields | +1–1.5 days |
| Crop (rect validation; the interactive crop box) | 3–4 days |
| **All five operations, number-field clip** | **~11.5 days total** |
| **All five, scrubber clip** | **~13 days total** |

Crop alone is a third of the non-foundation cost, entirely in its UI — same
conclusion as the earlier estimate, sharper now that the draft mechanics
around it are specified.

**Recommended build order:** foundation, then **downscale first** — it
proves the whole draft lifecycle (create, save, replace-old-on-success,
promote, discard) against the cheapest possible operation, with nothing new
on the FFmpeg side and no new frontend interaction beyond a dropdown. Convert
(mkv, then mp3) next — same reasoning. Clip and crop last, once the mechanism
is proven and the two harder UI investments are the only unknowns left.
