# Vendored player libraries

These are the unmodified distribution files of the two libraries the player is
built from, committed rather than fetched from a CDN. `index.html` loads them
with `<script defer>`; there is no bundler and this is not a reason to add one.

| File | Package | Version |
|---|---|---|
| `plyr.min.js`, `plyr.css`, `plyr.svg` | [plyr](https://github.com/sampotts/plyr) | 3.8.4 |
| `hls.min.js` | [hls.js](https://github.com/video-dev/hls.js) | 1.7.3 |

`blank.mp4` is Plyr's own 1.7 KB placeholder, loaded during `destroy()` to stop
an element from holding a dead connection open. `plyr.svg` is Plyr's icon
sprite. Both are pointed at from `style.css`/`app.js`: Plyr's defaults for them
are absolute `cdn.plyr.io` URLs, which would put a third party in the path of
every card toggle.

**Why vendored, not a CDN.** The app is deployed from Alibaba Cloud in
cn-hongkong and is used from mainland China, where both `cdn.plyr.io` and
`cdn.jsdelivr.net` are unreliable. These files decide whether any video plays
at all, so they are served from our own nginx next to `app.js`.

**Updating.** Fetch the same paths from the package's `dist/` directory at the
new version, replace the files, update the table above, and re-check the
quality-menu wiring in `app.js` — Plyr has no HLS support of its own, so that
integration goes through its `quality.forced` / `onChange` options rather than
a documented API.

Licences are in `plyr.LICENSE` (MIT) and `hls.LICENSE` (Apache-2.0).
