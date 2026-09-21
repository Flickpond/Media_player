# The two components the editor mounts

`app.js` owns the editing panel: the five operation sections, the request body,
and the polling. Two of those sections are not its own work — the crop box
(Track B) and the clip scrubber (Track C) — so it mounts them through the
contract below instead of implementing them.

```js
// Each is a global the panel looks up when the editor opens.
window.mountCropBox(video, onChange);      // Track B
window.mountClipScrubber(video, onChange); // Track C
```

**Both take the `<video>` element the selection is drawn against and a
callback, and call the callback whenever the user changes the selection.
Nothing else crosses the seam.** The panel owns the request body and the
Process button; the component owns its own DOM, its own drag maths, and its own
accessibility — including pausing or seeking that element, which is what a
crop box drawn over a paused frame needs.

| Component | Calls `onChange` with |
|---|---|
| Crop box | `{ x, y, w, h }` in the **source video's** pixels, not the element's |
| Clip scrubber | `{ start, end }` in seconds, `start < end` |

Returning a cleanup function is optional but supported — it is called when the
editor closes or opens another job, so a component that adds listeners to
anything outside its own container can remove them.

Three things the panel relies on, from `sprint3-plan.md` §1:

- **Nothing is sent while the user drags.** Both components preview client-side
  against the video already on the page; that is the whole reason the editing
  suite has no undo.
- **The video element is a fresh one per job.** The panel mounts a new player
  each time the editor opens, so a component is mounted once per editor
  session and never has to handle a source changing underneath it.
- **A component that is not on the page yet disables its section**, with a note
  saying whose work it is. That is deliberate: an operation with no parameters
  is a request the worker answers with a 422.

The video's real dimensions are on the element (`videoWidth`, `videoHeight`)
once it has metadata — that is the number the crop box has to convert to, and
the trap in `known-traps.md` about a 16:9 desktop clip looking perfect while a
vertical phone video is wrong everywhere else is exactly this conversion.
