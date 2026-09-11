# Video editing suite — design and plan

**Written:** 11 September 2026 · against `main` @ `9922ad0`
**Status:** design only. Nothing in this document is built yet.
**Revision 2:** switched from a sequential undo/redo chain to a single batch
job. §8 explains why — in short, none of these five operations need a real
server-rendered result to preview, so the cost of a chain (undo, redo, one
extra generation of lossy re-encoding per step) was buying a guarantee
nothing here actually needed.
**Revision 3:** operation order within a batch job is no longer
user-controlled. §4 explains why a fixed pipeline (clip, crop, scale,
convert — the size-reducing steps always ahead of the expensive ones) is
both the correct reading of what the crop UI captures and the faster order,
so there was nothing left for a user-chosen order to actually decide.

This is not part of the original proposal — crop/clip/scale/convert tools
aren't in `proposal.md`'s scope at all. Where it lands (sprint 3, replacing
some of the proposal's original sprint 3 content, or a new sprint 5) is a
team call; this document specifies *what* and *how*, not *when*.

Read [`known-traps.md`](known-traps.md) and [`contract.md`](contract.md)
before touching any of this — the same rule as every other plan in this repo.

---

## 1. The flow this is built for

1. A video finishes uploading and processing (the existing pipeline,
   unchanged). It's playable.
2. Below the player, the user configures whichever of the five operations
   they want — Crop, Downscale, Upscale, Clip, Convert — any subset, in any
   combination. Nothing is sent to the server yet.
3. Crop and Clip preview **client-side**, against the original video that's
   already loaded: a draggable box over a paused frame for crop, a scrubber
   over the timeline for clip. No processing, no round trip, so changing your
   mind costs nothing — this is what "undo" actually means here (§8).
4. The user presses **Process**. One job runs every configured operation in
   one FFmpeg pass — one decode, one encode, not one per operation — and
   produces one output.
5. The result appears as a normal library entry once done, exactly like an
   upload. The original is untouched and stays in the library separately, so
   a later session can start fresh from it.

One job, one set of parameters, one output. No draft state, no chain, no
promote step — the existing `queued -> processing -> done | failed` machinery
this project already has handles the whole thing unchanged.

---

## 2. Data model

Smaller than the sequential design needed — no new `kind`, no draft/library
distinction, no parent/origin chain. One column:

```sql
ALTER TABLE jobs ADD COLUMN operations JSONB;
```

`NULL` on a plain upload. On an edit job, the set of requested operations
and their params:

```json
[
  { "operation": "crop", "params": { "x": 0, "y": 140, "w": 1080, "h": 1080 } },
  { "operation": "downscale", "params": { "height": 480 } },
  { "operation": "convert", "params": { "format": "mkv" } }
]
```

Stored as a JSON array for convenience, but **the array's order is not
execution order** — the client sends whichever operations the user checked,
in whatever order they happened to check them, and the worker always runs
them in one fixed sequence regardless (§4). None of these five operations
actually have a meaningful *user* intent tied to relative order — crop and
clip are both always anchored to the *original* video, not to whatever an
earlier step in the chain produced — so there's nothing for the user to
control here, and no reason to make them think there is.

`source_key` already exists and works unchanged — an edit job's source is
whatever existing job the user started from (the original, or another
library entry). No `parent_job_id` needed for that alone: the existing job
row *is* the result, indistinguishable from an upload once it's `done`,
because nothing about how it's produced needs to be visible after the fact.

---

## 3. The one new endpoint

```
POST /jobs/{id}/edit
  body: { "operations": [ { "operation": "crop", "params": {...} }, ... ] }
  202 { "job_id": "<uuid>" }
  404 { "error": "not found" }        -- unknown id, or not this caller's
  422 { "detail": [...] }             -- empty list, unknown operation name,
                                          params fail that operation's
                                          validation, or downscale and
                                          upscale both present (§4)

  `id` is the source: the original, or any existing library entry. Creates a
  new job with source_key = id's output (or source, if id has none yet --
  editing a still-processing video isn't supported; the client should not
  offer these buttons until the source job is done). Processed exactly like
  an upload (queued -> processing -> done | failed) by the same worker,
  through the same GET /jobs/{id} polling the frontend already has.
```

That's the entire new API surface. No `/undo`, no `/redo`, no `/promote`, no
change to `list_jobs` or to what `GET /jobs` returns — an edit job is a job,
full stop.

---

## 4. Worker: one fixed pipeline order, not a user-configurable one

Still needs the same per-operation FFmpeg shapes as before, and still needs
`ffprobe` first for crop (source dimensions) and clip (source duration) —
that part of the earlier design didn't change:

| `operation` | `params` | FFmpeg shape | Needs `ffprobe` first? |
|---|---|---|---|
| `clip` | `{"start": 5.0, "end": 12.5}` | `-ss {start} -to {end}` (input-side flags, not a filter) | Yes — validate against source duration |
| `crop` | `{"x", "y", "w", "h"}` | `crop={w}:{h}:{x}:{y}` | Yes — validate rect fits inside source dimensions |
| `downscale` | `{"height": 480}` | `scale=-2:{height}` | No |
| `upscale` | `{"height": 1080}` | `scale=-2:{height}:flags=lanczos` | No |
| `convert` | `{"format": "mkv"}` / `{"format": "mp3"}` | remux (`-c copy`) or `-vn -c:a libmp3lame` | No |

The table is ordered the way the worker always builds the command, whichever
of these the client actually requested: **clip, then crop, then scale
(downscale or upscale), then convert.** Not user-configurable, and not
arbitrary — every step of it is either a correctness requirement or a
genuine performance win, usually both:

- **Clip first, structurally.** `-ss`/`-to` are input-side flags, not
  entries in the `-vf` filter graph, so they already bound how many frames
  reach *every* other step — crop, scale, and the encoder all only ever see
  the trimmed range, however the request lists its operations. This was
  already true in the previous revision; it's worth stating as policy now
  rather than an incidental property.
- **Crop before scale, always — this is a correctness requirement, not a
  preference.** The crop box in the UI is drawn against the *original*
  frame, so `x`/`y`/`w`/`h` are only meaningful in the original's coordinate
  space. Running scale first and crop second would need the rect rescaled to
  match, for no benefit. Anchoring crop to the original and always running
  it before any scale is simultaneously the only correct reading of what the
  user drew and the cheaper order: crop shrinks the pixel count *before* the
  more expensive scale-and-encode work runs, not after.
- **`downscale` and `upscale` are mutually exclusive in one job** — reject
  both present with a 422. There's no coherent reading of "shrink and
  enlarge in the same pass," and not deciding this up front is what would
  force an arbitrary tie-break later.
- **Convert last, unchanged from the previous revision** — it changes the
  container/codec, not the frame content, so it's the only step for which
  "before or after the others" was never a real question.

This is the concrete answer to "prioritise the operations that shrink the
video": crop and clip — the two operations that reduce pixel count and frame
count — always run before scale and encode, the two most expensive steps,
rather than after. It's not a heuristic bolted on top of a user-ordered
list; it falls out of making clip and crop's coordinate spaces correct in
the first place. The one place a genuine ambiguity could exist (`downscale`
vs. `upscale`) is removed by making the combination invalid rather than
guessed at.

A registry — the same `{"crop": build_crop_args, ...}` shape the earlier
design already planned — still replaces `get_processing_step()`'s single
hardcoded command, now assembling the fixed sequence above from whichever
entries the request actually included.

**Deliberately excluded: real (AI/super-resolution) upscaling.** Unchanged
from the earlier version of this document — a separate initiative with its
own deployment shape (GPU inference), not a variant of this one.

---

## 5. Frontend

Still one shared view, reachable from the Upload page's finished job and
from any Library entry — that part didn't change. What's gone: the
Save/poll-a-draft loop repeated per operation, and the Undo/Redo buttons.

- The player, unchanged.
- A panel listing the five operations, each collapsible/optional — check the
  ones you want, leave the rest alone. Crop shows the draggable box over a
  paused frame; Clip shows the trim range on a scrubber; both update purely
  client-side as the user drags, no request sent.
- Downscale/Upscale/Convert are plain dropdowns. Downscale and Upscale are
  mutually exclusive — selecting one disables the other, rather than
  letting the user configure a combination the worker will just reject.
- **Process** — enabled once at least one operation is configured. Calls
  `POST /jobs/{id}/edit` with whichever operations are checked; execution
  order is the worker's fixed sequence (§4), not something the UI needs to
  expose or let the user control. Polls exactly like the upload flow
  already does — same badge/progress-bar pattern, nothing new to build
  there.
- On success, the result is just a library entry — no separate "Save to
  Library" confirmation step, because nothing was ever in a provisional
  state to confirm out of. If the team wants a review moment before
  committing anyway, that's a one-line addition (start the result as
  unlisted/private rather than immediately visible), not a new mechanism.

The crop box remains the one genuinely new interactive component: a
draggable/resizable rectangle over a paused video frame, converting on-screen
pixels back to the source's real pixel coordinates, optionally locked to a
target aspect ratio. Worth a small library (e.g. cropperjs) rather than
hand-rolling drag math.

---

## 6. What this deliberately does not solve

- **`.mkv` playback.** A remux is nearly free server-side, but some browsers
  don't play `.mkv` in a plain `<video>` element as well as `.mp4` — a
  converted-to-mkv result may need to fall back to "Download" rather than
  "Watch inline." Flag this at the UI, don't silently break the player.
- **Real upscaling.** See §4.
- **Genuinely wanting to see a real rendered result before deciding the next
  step.** This design accepts that cost deliberately — see §8 — on the
  premise that none of these five operations need it. If a future operation
  actually is unpredictable from its parameters (a style filter, a color
  LUT), that operation is a real argument for revisiting a chain model for
  *it specifically*, not a reason to have kept one for all five here.

---

## 7. Effort

| Piece | Estimate |
|---|---|
| Schema: `operations` column | 0.25 day |
| `POST /jobs/{id}/edit`, validation (unknown op, bad params, downscale+upscale both present) | 1 day |
| Worker: registry, the fixed clip/crop/scale/convert pipeline, `ffprobe` step | 1.25 days |
| Frontend: shared edit view, operation panel, Process + polling | 1.25 days |
| **Foundation subtotal** | **~3.75 days** |
| Downscale / Upscale / Convert (each: dropdown + registry entry) | 0.5 day each, ~1.5 days combined |
| Clip (client-side scrubber preview + duration validation) | 1–1.5 days |
| Crop (client-side crop box + rect validation) | 2.5–3.5 days |
| **All five operations** | **~8.75–10.25 days total** |

Cheaper than the sequential design's ~12.5–14 days, for two stacking
reasons: no undo/redo endpoints, no draft/library distinction, no chain to
keep consistent (real removed mechanism, not just fewer lines) — and now
also no order-selection UI or reordering logic to build, since there was
never a meaningful order for a user to choose in the first place.

**Recommended build order:** foundation, then **downscale first**, same
reasoning as before — proves the one new endpoint and the worker's list
handling against the cheapest possible operation. Convert next. Clip and
crop last, since their client-side preview components are the real
remaining unknowns, not the backend.

---

## 8. Why batch instead of sequential (revision 2)

The first version of this document specified a sequential model — one
operation at a time, each producing a draft the next operation could build
on, with undo/redo to step back through real results. That's a legitimate
design and it's still in this file's git history if it's ever wanted, but
two things argued against it once actually weighed against what these five
operations are:

**Generation loss.** Every save re-encodes the video. Three sequential saves
(crop, then upscale, then clip) means three decode/encode passes, each
discarding information the previous encode already threw away — the same
effect as repeatedly re-saving a JPEG. One batch job with all three filters
in one pass is one generation of loss, not three, and that difference is
real in the output, not just an engineering nicety.

**None of these five operations need a real server render to preview.**
Sequential's whole justification is "see the actual result before deciding
the next step." But crop is a rectangle — previewable by drawing a box over
a paused frame. Clip is a time range — previewable by scrubbing the video
that's already loaded. Downscale, upscale, and convert are a number or a
format name with a fully predictable effect. None of these are the kind of
operation (an AI filter, a color-grade LUT) where the outcome is genuinely
unknown until it's rendered. A client-side preview gives the "know what
you're going to get" guarantee sequential was built to provide, for free,
without ever touching the server.

Undo, under batch, is free for the same reason: nothing is committed until
**Process** is pressed, so "changing your mind" is just editing a form.

This reasoning is specific to these five operations. It is not a general
argument that batch always beats sequential — an operation whose result is
genuinely unpredictable from its parameters would be a real case for a
chain, for that operation.

---

## 9. The video player — a separate, smaller, shippable-now upgrade

Unaffected by the batch/sequential decision above — this section is
unchanged from the previous revision. Not part of the editing suite's cost,
and doesn't need any of it. Every place a video plays today — the Upload
page's active-job card, Library's click-to-expand, Admin's Watch toggle —
uses a bare `<video controls>`, and the native controls it renders differ by
browser, look dated, and don't support anything past play/pause/seek/volume
(no playback speed, no scrub-preview, inconsistent fullscreen and keyboard
behaviour). Worth fixing regardless of whether the editing suite above ever
gets built, and cheaply:

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
