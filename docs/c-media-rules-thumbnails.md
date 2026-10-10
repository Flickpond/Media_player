# Sprint 4 track C: media rules and thumbnails

Owner: Yang Dongwei (`@ttydw-ch`). Branch: `C-status-endpoints-db`.
Baseline: `main` at `5de6b4b`, 4 October 2026.
Plan: [`sprint4-plan.md`](sprint4-plan.md), track C (Mon–Wed items).

## What this delivers

| Plan item | Where |
| --- | --- |
| Duration limit; probe-first rejection of too-long, corrupt and video-less files | `app/services/media_rules.py`, `app/worker/validation.py`, wired into `FfmpegProcessor` |
| Thumbnail: one frame right after the probe; `thumbnail_url` in the API | `app/worker/thumbnail.py`, `FfmpegProcessor`, `mark_done`, `GET /jobs/{id}` |
| Source-aware scale rules; `/edit` returns 422 at once from stored dimensions | `app/services/media_rules.py`, `POST /jobs/{id}/edit`, `edit_options` on the job |
| The limit the page shows in minutes | `GET /limits` |

**Not in this change: editing from the original upload.** The plan pairs it
with E's crop box (section 3): both ship together or neither does, decided
on Wednesday. Edits still start from the MP4, and every rule below is written
against that. When the go decision is made, only `editable_height()` and the
edit endpoint's `source_key` change.

No migration: A's `20261004_04` already added `thumbnail_key`, the dimensions
and `hls_status`.

## For E: what the page can read

```text
GET /limits            -> { "max_duration_seconds": 300, "max_edit_height": 2160 }
GET /jobs/{id}         -> ..., "thumbnail_url"?, "edit_options"?
POST /jobs/{id}/edit   -> 422 { "error": "<one readable line>" } for a media rule
```

- **`thumbnail_url`**: a signed JPEG URL on done uploads. Absent while
  processing, on failed jobs, on edit jobs, and when the frame could not be
  taken -- show a placeholder for all of those.
- **`edit_options`**: `{ "downscale": [...], "upscale": [...] }` -- build the
  two dropdowns from these instead of fixed lists. An empty list means hide
  that operation (a 2160p video gets no upscale). Absent when the job is not
  done or has no stored dimensions: keep the current fixed lists then; the
  worker still checks.
- With a crop selected, scale is judged against the **crop's** height. If the
  crop box lands this sprint, filter `edit_options` against `crop.h` the same
  way (rungs below it for downscale, above it for upscale).
- `max_duration_seconds` lets the picker say "up to 5 minutes" and reject a
  long file in the browser (from `<video>` metadata) before uploading it.

## For A: what changed in `app/worker/*`

All in files track A owns, so flagged here for review:

- **`probe.py` reports display dimensions.** A phone video recorded upright
  is stored as landscape frames plus a rotation flag. ffprobe reports the
  stored 1920x1080; FFmpeg rotates before any filter, so the MP4, the ladder
  and the thumbnail are all 1080x1920. The probe now reads the rotation
  (`stream_side_data=rotation`, and the legacy `rotate` tag) and swaps width
  and height for ±90°. Checked against real FFmpeg in
  `tests/integration/test_media_rules_ffmpeg.py`. Without this, an upright
  4K phone video's ladder would be capped at 2160 when its frames are 3840
  tall. A file with no video stream now gets its own message.
- **`storage.py`**: `FfmpegProcessor` takes two optional seams, both wired in
  `get_processing_step()`:
  - `source_check(probe)` -- C's rules. When given, the probe is no longer
    best-effort: a probe failure or a rule failure fails the job before
    encoding. Without it, the old tolerant behaviour is unchanged.
  - `thumbnailer(source_path, output_path, probe)` -- runs after the probe;
    the JPEG is uploaded only after the MP4, so a failed encode leaves no
    unreferenced object. Any thumbnail failure is logged and skipped.
  - `ProcessingResult.thumbnail_key`, passed to `mark_done` by `tasks.py`.
- **`operations.py`** (B's) now probes for scale operations too and calls
  `validate_edit_rules` after crop/clip validation -- the same seam as
  sprint 3.

## Decisions to confirm in the group

1. **Duration limit: 5 minutes** (`MEDIA_MAX_DURATION_SECONDS=300`). A guess
   until A times a real 4K clip on the staging instance on Thursday; it is
   one environment variable to change.
2. **Scale rungs: 240, 360, 480, 720, 1080, 1440, 2160.** Arbitrary heights
   are refused -- an odd height fails inside libx264 mid-encode, and the
   dropdowns can only offer fixed values anyway.
3. **The MP4 cap is read from `WORKER_FFMPEG_MAX_HEIGHT`** -- now 1080 after
   #56, and `edit_options` follow it. An MP4 encoded under the old 720 cap is
   caught by the worker's own check against the real file. An edit job's
   options come from its own probed output (#56 stores those dimensions).
4. **Editing from the original: no-go for sprint 4.** Moves to sprint 5 with
   E's crop box, per the pairing rule.

## Tests

```text
tests/test_media_rules.py                 rules, limits, options <-> rules agreement
tests/test_media_rules_api.py             thumbnail_url, edit_options, 422s, /limits, delete
tests/test_worker_source_rules.py         probe-first rejection and thumbnail ordering
tests/test_worker_thumbnail.py            frame extraction and its failure modes
tests/test_worker_probe.py                rotation handling (added cases)
tests/integration/test_jobs_repository.py thumbnail_key round trip (added cases)
tests/integration/test_media_rules_ffmpeg.py   real FFmpeg: upright phone video, limit, audio-only
```
