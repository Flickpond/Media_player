# The components the editor mounts

`app.js` owns the editing panel: the five operation sections, the request body,
and the polling. Two of those sections are not its own work — the crop box
(Track B) and the clip scrubber (Track C) — so it mounts them through the
contract below instead of implementing them.

```js
// Each is a global the panel looks up when the editor opens.
window.mountCropBox(video, onChange, { width, height }); // crop-box.js
window.mountClipScrubber(video, onChange);                // clip-scrubber.js, Track C
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

**Edits start from the original upload** (sprint 5). When the clicked job is
itself an edit, the panel fetches `GET /jobs/{edit_source.job_id}` and plays
that, so crop is drawn in the original's pixels and clip runs on the original's
timeline. Until it arrives both sections stay disabled.

The crop box's third argument is the original's real size, from
`edit_source`. It is not optional in practice: `videoWidth` and `videoHeight`
on the element are only the rendition playing *right now*, and an adaptive
ladder can start a 4K upload at 360p. The element's own size is the fallback
for a job the API sent no size for. Getting this conversion wrong is the trap
in `known-traps.md` about a 16:9 desktop clip looking perfect while a vertical
phone video is wrong everywhere else.

The crop box reports even, whole numbers: libx264 refuses an odd-sized 4:2:0
picture, and the worker's `validate_crop` refuses anything but integers.

## Operations added since sprint 6

A new operation needs no change to `app.js`. Its section in `index.html`
names the operation and the global to mount:

```html
<details class="edit-op" data-operation="rotate" data-mount="mountRotatePanel">
  <summary><input type="checkbox" id="edit-rotate" /> <span class="edit-op-name">Rotate</span></summary>
  <div class="edit-op-body"><div class="edit-mount"></div></div>
</details>
```

```js
window.mountRotatePanel(video, onChange, { width, height, duration });
```

`app.js` finds every `details[data-operation][data-mount]` when it loads, and
for each one:
- mounts the global on the editor's video, or shows "Coming in the next
  update" if the global is not there;
- resets the checkbox when the editor opens;
- calls the cleanup when it closes;
- sends `{ operation: <data-operation>, params: <what onChange last reported> }`
  when the box is ticked.

The third argument is the original's size and length (`edit_source`), so a
preview can be drawn to scale before the element has metadata. Step by step,
with the server side: [`docs/adding-an-edit-operation.md`](../docs/adding-an-edit-operation.md).
