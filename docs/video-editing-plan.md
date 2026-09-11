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
4. The user can press **Undo** to go back one step — to the previous draft,
   or to the original if there was no previous draft. **Redo** goes forward
   again, as long as nothing new was saved in between (§4).
5. This repeats for as many operations as the user wants.
6. At any point, the user can discard the whole editing session and go back
   to the untouched original — which was never modified by any of this.
7. When satisfied, the user presses **Save to Library**. The step they're
   currently looking at becomes a real library entry — downloadable, listed,
   playable like any other video. Every other draft from that session (the
   ones undo could still reach, the ones redo could still reach) is deleted.
   The original stays in the library too, separately, so a later editing
   session can start fresh from it.

An undo/redo *chain* per original, not a single replaceable slot — that
changed once undo entered the picture (§4 explains why the earlier
single-slot design doesn't support it). Cleaned up only at the edges of a
session — Save to Library, or discarding it entirely — never mid-session.
Everything below exists to make that correct under real conditions: a step
that fails, a browser closed mid-edit, two tabs open on the same video, and
now, undoing partway and then taking a new step from there.

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
ALTER TABLE jobs ADD COLUMN is_current_draft BOOLEAN NOT NULL DEFAULT false;

ALTER TABLE jobs ADD CONSTRAINT ck_jobs_kind
  CHECK (kind IN ('upload', 'draft', 'library'));

-- The load-bearing constraint, unchanged in spirit from the single-slot
-- design: the database refuses a second *current* draft for the same
-- original, so two tabs racing to save can't both succeed and leave two
-- drafts both claiming to be "where the user is" -- one of the two writes
-- just fails. What changed is that this no longer says "at most one draft
-- per origin" -- it says "at most one *current* one". Past drafts in the
-- undo chain are allowed to coexist; only one of them is ever the one the
-- user is currently looking at.
CREATE UNIQUE INDEX ux_jobs_one_current_draft_per_origin
  ON jobs (origin_job_id) WHERE kind = 'draft' AND is_current_draft;
```

- **`kind`** — `upload` (an original, exactly today's rows), `draft` (any
  step in an undo/redo chain, invisible to `GET /jobs`, current or not),
  `library` (a promoted, permanent entry — same visibility as `upload`).
- **`parent_job_id`** — the immediate source: the job this one was derived
  from. A draft's parent can be the original or another draft. This is the
  undo chain -- walking `parent_job_id` backward from any draft always
  reaches the original.
- **`origin_job_id`** — the root original this whole chain traces back to.
  `NULL` on an `upload` row (it *is* the origin); always a concrete id on a
  `draft` or promoted `library` row. This is what the unique index enforces
  against, and what "find every draft in this session" queries against
  directly, without walking `parent_job_id` chains one row at a time.
- **`is_current_draft`** — exactly one `true` row per `origin_job_id` among
  its drafts, or none, if the user is looking at the original itself. Undo
  and redo move which row this is; neither one deletes anything (§4).
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
  own origin_job_id if it has one, else id itself; is_current_draft = true.
  The previous current draft (if any) has is_current_draft set false in the
  same transaction -- see the branch-pruning note in §4 for what else that
  transaction has to check. Processed exactly like an upload
  (queued -> processing -> done | failed) by the same worker.

POST /jobs/{id}/undo
  204                                 -- id must be the current draft
  404 { "error": "not found" }
  409 { "error": "nothing to undo" }  -- id's parent is not a draft or the
                                          origin the caller owns

  Moves is_current_draft from `id` to `id`'s parent -- if the parent is a
  draft, that draft becomes current; if the parent is the original, no draft
  is current at all, and the client falls back to showing the original.
  Deletes nothing.

POST /jobs/{id}/redo
  204                                 -- id must be the origin, or the
                                          current draft, and must have a
                                          child created by an edit (not by
                                          promotion or deletion)
  404 { "error": "not found" }
  409 { "error": "nothing to redo" }  -- undone once and then a new edit was
                                          made from here, which prunes this
                                          branch (§4) -- or nothing was ever
                                          undone

  The reverse of undo: moves is_current_draft to the child that undo most
  recently moved away from.

POST /jobs/{id}/promote
  204                                 -- id must be a `draft` this caller owns,
                                          status = done
  404 { "error": "not found" }
  409 { "error": "job is not a finished draft" }

  Flips kind: 'draft' -> 'library' for `id`. Deletes every *other* draft
  sharing its origin_job_id -- the rest of the undo/redo chain, now
  abandoned. No new processing for the promoted row -- the object already
  exists, this only changes what GET /jobs returns.

GET /jobs/{id}
  -- extended, not replaced. When `id` is an origin (upload or library) with
  -- a current draft, the response gains one field:
  200 { ..., "draft"?: { "id", "status", "output_url"?, "error"?,
                          "can_undo", "can_redo" } }
```

**Discarding the whole session needs no new endpoint** — `DELETE /jobs/{id}`
(already built) generalises to delete `id` and, where `id` is an origin,
every draft under it (a small extension to the existing repository call, not
a new one). "Go back to the original" this way is permanent, unlike undo:
nothing in the session is recoverable afterward.

---

## 4. Undo, redo, and what actually gets deleted

The first pass at this design deleted the previous draft the instant a new
one succeeded — a single replaceable slot. **Undo doesn't work under that
design**: once step 2 replaces step 1, step 1 is gone, and there's nothing
left to undo *to*. Supporting undo means keeping the chain, not the slot.

**So nothing is deleted mid-session.** Every draft a user creates stays in
the database and in storage until the session ends — either by `promote`
(keep the current one, delete the rest of the chain) or by deleting the
origin outright (keep nothing). This is a real, if temporary, storage cost:
a session with five edits holds five draft videos at once, not one. It's
bounded by *a finished session*, though, not a permanent leak — but a session
nobody ever finishes (closes the tab mid-edit and never comes back) is a real
gap this design doesn't close on its own; see §7.

**Undo moves a pointer; it does not delete anything.** `is_current_draft`
moves from the current row to its parent. The row being stepped away from is
untouched — which is exactly what makes redo possible: redo just moves the
pointer back to the child undo came from, as long as nothing has grown a new
branch from that point since.

**Taking a new step after an undo prunes what undo could have redone into.**
This is the one genuinely new rule undo introduces, and it's the same rule
every editor with undo/redo already uses: if the user undoes back to step 2,
then performs a *different* operation from there instead of redoing step 3,
step 3 (and anything that would have come after it) stops being reachable —
redo has nothing left to reach. `POST /jobs/{id}/edit` has to check for this:
before creating the new draft, delete any existing child of `id` that isn't
the one about to be created. Skipping this check doesn't corrupt anything —
it just leaves an abandoned branch sitting in storage that promote's
end-of-chain cleanup (§3) will eventually catch regardless, since it shares
the same `origin_job_id` — but it's cheap to do promptly and worth doing so
the DB doesn't accumulate abandoned branches for the length of a long
session.

**A step that fails changes none of this.** A `POST /jobs/{id}/edit` whose
job ends in `failed` never touches `is_current_draft` — the failed row is
just a `draft` with `status='failed'` hanging off `id` as a child, visible
in `GET /jobs/{id}`'s `draft` field the normal way, offering nothing to undo
into. The user's current draft stays exactly what it was before they tried.

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
- **Undo** / **Redo** — call the endpoints in §3 directly; both are instant
  (a pointer move, not a processing job), so no polling. Enabled/disabled off
  `draft.can_undo` / `draft.can_redo` from the last `GET /jobs/{id}`, not
  guessed client-side — the server is the one that knows whether the current
  row has a usable parent or a live redo branch.
- **Save to Library** — visible whenever a draft exists; calls
  `POST /jobs/{id}/promote`, then returns to Library.
- **Discard the session** / **back to original** — `DELETE` on the origin.
  Unlike undo, this is permanent: say so in the confirmation, the same way
  every other delete in this app already asks before acting.
- On opening a video that already has a current draft (`GET /jobs/{id}`'s
  `draft` field), resume showing that draft, not the original — someone who
  left mid-edit and came back should see where they left off, undo/redo
  state included.

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
- **A session nobody ever finishes.** The existing reaper sweeps stale
  `processing` rows and storage objects that no row points to at all — an
  abandoned draft is neither: it's a finished, valid `done` row someone
  simply never promoted or discarded. Nothing built here or already in the
  reaper cleans that up, so it's a real, if slow, storage leak until
  something does — most likely a small addition to the reaper's sweep
  (`kind = 'draft' AND updated_at < now() - interval`), not solved by this
  document and worth its own small follow-up rather than silently assuming
  away.

---

## 8. Effort

Foundation, revised for the chain-based design — cheaper on the worker side
than the single-slot version (there's no delete-on-success step mid-session
at all now), more expensive on the API side (two new endpoints, and the
branch-pruning check):

| Piece | Estimate |
|---|---|
| Schema migration, `kind`/parent/origin/`is_current_draft` columns, the unique-current-draft index | 0.5 day |
| `POST /jobs/{id}/edit` (incl. branch pruning) + `/undo` + `/redo` + `/promote`, `list_jobs` excluding drafts | 2 days |
| Worker: operation registry, `ffprobe` step | 1 day |
| Frontend: shared edit view, Save / Undo / Redo / Save to Library / Discard, resume-a-session | 2 days |
| **Foundation subtotal** | **~5.5 days** |
| Downscale (dropdown + registry entry) | 0.5 day |
| Upscale, filter-based (dropdown + registry entry) | 0.5 day |
| Convert: mp4→mkv (registry entry; mkv playback caveat handling) | 0.5 day |
| Convert: mp4→mp3 (registry entry; `<audio>` render path for an audio result) | 1 day |
| Clip (duration validation; two number fields) | 1 day |
| Clip, with a real scrubber instead of number fields | +1–1.5 days |
| Crop (rect validation; the interactive crop box) | 3–4 days |
| **All five operations, number-field clip** | **~12.5 days total** |
| **All five, scrubber clip** | **~14 days total** |

Crop alone is still close to a third of the non-foundation cost, entirely in
its UI. Undo/redo added about a day net to the foundation, not more — most
of that cost is the API surface, not new mechanism, since the chain
underneath was already shaped like an undo history once drafts stopped being
deleted eagerly.

**Recommended build order:** foundation, then **downscale first** — it
proves the whole chain (create, save, undo, redo, promote, discard) against
the cheapest possible operation, with nothing new on the FFmpeg side and no
new frontend interaction beyond a dropdown. Convert (mkv, then mp3) next —
same reasoning. Clip and crop last, once the mechanism is proven and the two
harder UI investments are the only unknowns left.

---

## 9. The video player — a separate, smaller, shippable-now upgrade

Not part of the editing suite's cost above, and doesn't need any of it. Every
place a video plays today — the Upload page's active-job card, Library's
click-to-expand, Admin's Watch toggle — uses a bare `<video controls>`, and
the native controls it renders differ by browser, look dated, and don't
support anything past play/pause/seek/volume (no playback speed, no
scrub-preview, inconsistent fullscreen and keyboard behaviour). Worth fixing
regardless of whether the editing suite above ever gets built, and cheaply:

- **Recommend [Plyr](https://plyr.io/)** over hand-rolling controls or
  reaching for something heavier like video.js. It wraps the existing
  `<video>` element rather than replacing it — the same `output_url` becomes
  its source, nothing changes server-side — ships a CDN-friendly build (this
  project has no bundler and isn't getting one for this), and themes through
  plain CSS custom properties, which maps cleanly onto the tokens
  `style.css` already defines (`--accent`, `--bg-raised`, etc.).
- **One real wrinkle**: several of the three places a video plays create and
  destroy `<video>` elements dynamically (Library's toggle-to-play, Admin's
  Watch/Hide). Plyr instances need an explicit `.destroy()` when their
  element is removed, or they leak — the toggle helpers already in
  `frontend/app.js` (`toggleInlinePlayer`) are the one place this needs to be
  threaded through, not four separate places.
- **Effort: ~1 day** — include the library, initialise it wherever a
  `<video>` is created instead of leaving it bare, restyle to the existing
  dark/blue tokens, handle the destroy-on-toggle wrinkle above. Small enough
  to do on its own, independent of everything else in this document, and
  worth doing first if the team wants a visible win before the bigger
  editing-suite build.
