# Adding an edit operation

For sprint 6's new operations — `rotate`/`flip`/`speed` (B), `adjust` (C),
`preset` (E) — and any after them. The contract is in
[`contract.md`](contract.md) ("Sprint 6: the edit-operation seam"); this is
the walk-through. `crop` and `downscale`/`upscale` are already built this
way: read them first.

An operation is **four pieces, each in your own files**, plus one line in
each of four shared places. Nobody else's code changes.

| # | Piece | Your file | One shared line |
|---|---|---|---|
| 1 | Parameters | `app/schemas/edit.py` (your model and operation class) | add it to the `EditOperation` union |
| 2 | Rule | your function in `app/services/media_rules.py` | `register_rule(...)` at the bottom |
| 3 | Filters | `app/worker/edit_ops/<name>.py` | import it in `edit_ops/__init__.py` |
| 4 | Panel | `frontend/<name>.js` + `.css` | a `<details>` section and a `<script>` in `index.html` |

Your operation already has its place in `OPERATION_ORDER`; don't move it.

---

## Worked example: `rotate`

### 1. Parameters — `app/schemas/edit.py`

```python
class RotateParams(_StrictModel):
    degrees: Literal[90, 180, 270]


class RotateOperation(_StrictModel):
    operation: Literal["rotate"]
    params: RotateParams
```

Then add `| RotateOperation` to the `EditOperation` union. `_StrictModel`
refuses unknown fields, so `{"degrees": 90, "x": 1}` is a 422 for free.

### 2. Rule — `app/services/media_rules.py`

The rule runs twice: in the API against the stored size (a 422 before any
job exists) and in the worker against the downloaded file. It receives the
frame as the operations before it left it, and returns the frame after it.

```python
def _rotate_rule(params: dict, frame: Frame) -> Frame:
    if params.get("degrees") in (90, 270):
        return Frame(width=frame.height, height=frame.width)
    return frame


register_rule("rotate", OperationRule(label="rotate", check=_rotate_rule))
```

Raise `MediaRuleError("…")` for a request this frame cannot take; its text is
shown to the user as written. `changes_picture=False` only for an operation
an MP3 can take (speed); everything else is refused with MP3 automatically.

### 3. Filters — `app/worker/edit_ops/rotate.py`

```python
"""Rotate: turn the picture a quarter, half or three-quarter turn clockwise."""

from app.services.media_rules import Frame
from app.worker.edit_ops.registry import FilterStep, register_filter

_TRANSPOSE = {90: ("transpose=1",), 180: ("transpose=1", "transpose=1"), 270: ("transpose=2",)}


def build(params: dict, _frame: Frame) -> FilterStep:
    return FilterStep(video=_TRANSPOSE[params["degrees"]])


register_filter("rotate", build)
```

and add `rotate` to the import line in `app/worker/edit_ops/__init__.py`.
`video` filters go into `-vf`, `audio` into `-af`. Yours land in the right
place in the chain — after crop, before scale; colour after the HDR
tone-map — whatever order the request listed them.

### 4. Panel — `frontend/rotate.js`

```html
<!-- index.html, inside <form id="edit-form">, before the Convert section -->
<details class="edit-op" data-operation="rotate" data-mount="mountRotatePanel">
  <summary>
    <input type="checkbox" id="edit-rotate" />
    <span class="edit-op-name">Rotate</span>
    <span class="edit-op-hint">a quarter turn at a time</span>
  </summary>
  <div class="edit-op-body"><div class="edit-mount"></div></div>
</details>

<!-- with the other scripts, before app.js -->
<script src="rotate.js" defer></script>
```

```js
// frontend/rotate.js
(function () {
  "use strict";
  // frame: { width, height, duration } of the original, when known.
  function mountRotatePanel(video, onChange, frame) {
    const host = document.querySelector('[data-operation="rotate"] .edit-mount');
    // …buttons; on a choice: onChange({ degrees: 90 })
    //   and preview it: video.style.transform = "rotate(90deg)"
    return function unmount() {
      video.style.transform = "";
      // remove listeners and your DOM
    };
  }
  window.mountRotatePanel = mountRotatePanel;
})();
```

Whatever you pass to `onChange` is sent as the operation's `params`, exactly.
Until you call it, a ticked box sends nothing and Process stays disabled.
Until your script is on the page, the section shows "Coming in the next
update". **Undo every change to `video` in your cleanup**: the element is
shared with the other panels.

---

## Tests — what each PR needs

- **Schema:** a valid request parses; each out-of-range value is a 422.
- **Rule:** the frame after it (for rotate: width and height swapped), and
  each refusal's message. See `tests/test_edit_ops_seam.py`.
- **Filters:** the exact `-vf`/`-af` for each parameter, and where they land
  in a full chain with crop and scale (`filter_chain`).
- **Real FFmpeg once:** run your command on a short clip and probe the result
  (size, duration). Say in the PR what you ran.
- **Panel:** `frontend/<name>.test.js`, like `crop-box.test.js`: what
  `onChange` reports, keyboard use, and that cleanup undoes the preview.
- Add your `.js` to `--coverage.include` in `frontend/package.json` and to
  `sonar.sources` / `sonar.tests` in `sonar-project.properties`.

## Checklist before review

- [ ] `python -m ruff check .` and `python -m pytest -q` pass
- [ ] `cd frontend && npm run test:coverage` passes (90% gate)
- [ ] Tried on staging (https://47-128-86-77.sslip.io) with a real upload
- [ ] The PR says which shared lines you added — they need no permission
