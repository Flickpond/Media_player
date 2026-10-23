# Sprint 6 B: rotate, mirror and speed

Branch: `b-sprint6-rotate-speed`, based on `origin/main` at `347df03`.

Adds three operations through A's existing edit-operation seam. No changes to
the API handlers, job model, database, queue, worker processor, `app.js`, crop
box, clip scrubber, deployment or infrastructure are required.

## Behavior

```json
{"operations":[
  {"operation":"clip","params":{"start":2,"end":6}},
  {"operation":"rotate","params":{"degrees":90}},
  {"operation":"flip","params":{"axis":"horizontal"}},
  {"operation":"speed","params":{"factor":0.5}}
]}
```

- Rotation is clockwise, strictly integer 90/180/270. Quarter turns swap
  width and height before the API and worker check subsequent scale requests.
- Mirror takes exactly one axis, `horizontal` or `vertical`, relative to the
  rotated picture. The existing contract permits one flip per request.
- Speed accepts 0.5 through 2 in increments of 0.25. Strings, booleans,
  nonfinite values, extra fields and unsupported increments are refused.
- The existing fixed pipeline puts clip/crop before rotate/flip, scale after
  them, and speed after the picture operations. Request order cannot change it.
  Clip times continue to refer to the original timeline. Speed 0.5 makes the
  selected four seconds above last approximately eight seconds.
- Speed uses `setpts=(PTS-STARTPTS)/factor` and
  `asetpts=PTS-STARTPTS,atempo=factor`. `atempo` preserves pitch and natively
  supports the entire 0.5-2 range; chaining is unnecessary at these factors.
  Silent videos work too. Speed with MP3 is supported; rotate/flip with MP3
  returns 422 through the shared picture-operation rule. Filtered MKV edits
  re-encode rather than silently stream-copying the unchanged source.
- The panels use `mountRotatePanel`, `mountFlipPanel`, `mountSpeedPanel`.
  Each reports its strict parameters through the existing callback. Preview is
  active only while its operation checkbox is checked. Rotation and mirror
  compose in server order and fit within the video area. Speed uses the
  browser's pitch-preserving playback. Native buttons/selects support keyboard
  use. Unmount removes listeners and restores transform, playback rate and
  pitch settings; other panels' CSS filters remain untouched.

## Shared-file additions for review

Most implementation lives in new B files: `edit_ops/rotate.py`, `flip.py`,
`speed.py`, `frontend/motion.js`, `motion.css`, and dedicated tests.

| Shared file | Change |
| --- | --- |
| `app/schemas/edit.py` | Three parameter/operation models and union entries |
| `app/services/media_rules.py` | Rotation frame rule and three registrations |
| `app/worker/edit_ops/__init__.py` | Import the three filter modules |
| `frontend/index.html` | Three plugin sections, one stylesheet and one script |
| `frontend/package.json` | Include the new panel in the existing coverage gate |
| `sonar-project.properties` | Include the new source and test |
| `tests/test_edit_ops_seam.py` | Isolate and restore temporary test registrations now that real rotate/speed exist; use an unregistered test name for the missing-rule check |
| `frontend/app.player.integration.test.js` | Add the new script to the expected page script order |

These are the registrations and acceptance-test adaptations needed to use the
merged seam. Existing operation implementations are unchanged.

## Local verification (21 October 2026)

Run backend commands in the project's Python 3.12 WSL environment:

```sh
python -m ruff check .
python -m pytest --cov=app -q
RUN_POSTGRES_TESTS=1 python -m pytest tests/integration/test_motion_ffmpeg.py -q
cd frontend
npm ci
npm run test:coverage
```

- Ruff: passed.
- Backend: **875 passed, 93 skipped**, coverage **91.82%**, above the 80% gate.
  One existing short-test-JWT-key warning. The normal run skips gated live
  integration tests; no database or storage acceptance is claimed here.
- Real FFmpeg/ffprobe: **15 passed**, using a generated 320x240 six-second
  red/blue clip with 440 Hz audio. Checks all rotation sizes, mirror pixels,
  seven clip-plus-speed durations and pitch (within 5 Hz), silent video,
  MP3 conversion, and crop/rotate/scale/speed together. This file needs only
  FFmpeg/ffprobe; it retains the repository's `RUN_POSTGRES_TESTS` guard.
- Frontend: **261 passed, 3 skipped**; total statement coverage **98.56%**,
  branch coverage **94.88%**. `motion.js` has **100%** statement, branch,
  function and line coverage. The existing 90% gate passed.
- Additional independent headless Chrome smoke, with a real generated video:
  Enter selects 180 degrees, Space selects vertical mirror, 0.5x changes
  playback with pitch preservation, and cleanup restores the original state.
  A local panel harness was used; this is not a deployed-site or full-app
  end-to-end acceptance claim.

## Remaining deployment acceptance

No staging/production deployment, remote verification, push or PR was performed
in this local implementation task. The plan's staging and production acceptance
remain to be recorded by the deployment owner:

1. Deploy this branch through the team's normal reviewed process, loading
   `motion.css` and `motion.js` before `app.js`.
2. On staging, upload a landscape and portrait video with recognizable corners
   and speech. Try every rotation and mirror; compare preview and output.
3. Submit crop + 90-degree rotation + scale, with operations reordered in the
   payload too. Confirm crop uses original coordinates and scale uses the
   rotated height.
4. Clip a known original interval, try 0.5x/1.5x/2x, and check output duration,
   unchanged speech pitch and audio/video synchronization. Repeat without audio
   and with MKV/MP3 conversion where allowed.
5. Disable/re-enable the operation checkboxes; close/reopen the editor. Confirm
   previews reset and Process submits only checked operations. Test keyboard
   controls and coexistence with colour/preset panels when those are merged.
6. After merge and production deployment, repeat rotate + speed on a real
   upload and record the date and job IDs. Do not mark these checks complete
   from the mocked API or local media results above.

## Commit handoff

1. `91a0e3a`: strict rotate/flip schemas, rules, filters and unit tests.
2. `73b77ac`: pitch-preserving speed schema, rule, filter and unit tests.
3. `d22234c`: independent panels, HTML registrations and frontend tests.
4. Final verification commit: API acceptance tests, real-media tests and this
   handoff record.

Push manually when ready:

```sh
git push -u origin b-sprint6-rotate-speed
```
