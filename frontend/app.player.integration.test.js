// @vitest-environment jsdom
//
// Integration tests for the player: the real vendored Plyr runs against the
// real index.html and the real app.js, with nothing stubbed but the network.
//
// app.test.js drives the app with stand-ins for both libraries. That is fast
// and it pins the app's own logic, but it is blind to everything only the real
// files can get wrong -- the shape of the config Plyr actually accepts, what
// destroy() really leaves behind, and whether the paths index.html advertises
// resolve to files that exist. This file is that layer; app.live.test.js is
// the one above it, and needs a running stack.
//
// hls.js is loaded here for its API and for its own fallback answer, but its
// playback path is not exercised: jsdom has no MediaSource, so the real
// library reports itself unsupported (which is itself worth asserting). The
// level plumbing is covered with a stand-in where noted.

import { existsSync, readFileSync } from "node:fs";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

const here = dirname(fileURLToPath(import.meta.url));
const markup = readFileSync(join(here, "index.html"), "utf8");
const BODY = markup.slice(markup.indexOf("<body>") + 6, markup.indexOf("</body>"));

/** Evaluate a vendor file the way the page does: as a classic script in the
 *  page's own realm, so what the tests exercise is the shipped file rather
 *  than something rebuilt for testing. */
function loadScript(relativePath) {
  (0, eval)(readFileSync(join(here, relativePath), "utf8"));
}

const jsonResponse = (body, ok = true, status = 200) => ({
  ok,
  status,
  json: async () => body,
});

const PLAIN = {
  id: "1",
  filename: "holiday.mp4",
  status: "done",
  output_url: "http://minio/holiday.mp4",
};
const LADDER = {
  id: "2",
  filename: "vertical.mp4",
  status: "done",
  output_url: "http://minio/vertical.mp4",
  hls_url: "/api/jobs/2/hls/master.m3u8",
};

let fetchMock;

/** Load the real page and the real app, signed in. */
async function loadPage({ role = "user" } = {}) {
  document.body.innerHTML = BODY;
  vi.resetModules();

  // The places jsdom stops short of a browser, all of them inside Plyr's own
  // setup rather than in the app: it asks about reduced motion, reads the
  // TextTrack interface while wiring captions, and calls load() to cancel
  // requests on destroy.
  globalThis.TextTrack ??= class TextTrack {};
  window.matchMedia ??= () => ({ matches: false, addEventListener() {}, removeEventListener() {} });
  HTMLMediaElement.prototype.load = () => {};

  loadScript("vendor/plyr.min.js");

  fetchMock = vi.fn();
  globalThis.fetch = fetchMock;
  fetchMock.mockResolvedValueOnce(
    jsonResponse({ id: "u-1", email: "maya@example.test", role }),
  );
  await import("./app.js");
  await vi.waitFor(() => expect(fetchMock).toHaveBeenCalledWith("/api/auth/me"));
  await vi.advanceTimersByTimeAsync(0);
  fetchMock.mockClear();

  return {
    navLibrary: document.getElementById("nav-library"),
    navAdmin: document.getElementById("nav-admin"),
    libraryGrid: document.getElementById("library-grid"),
    adminTbody: document.getElementById("admin-tbody"),
    player: document.getElementById("player"),
    fileInput: document.getElementById("file-input"),
  };
}

async function openLibrary(el, jobs) {
  fetchMock.mockResolvedValueOnce(jsonResponse(jobs));
  el.navLibrary.dispatchEvent(new Event("click"));
  await vi.waitFor(() => expect(fetchMock).toHaveBeenCalled());
  await vi.advanceTimersByTimeAsync(0);
}

function cardFor(el, filename) {
  return [...el.libraryGrid.querySelectorAll(".video-card")].find(
    (c) => c.querySelector(".video-filename").textContent === filename,
  );
}

/** Open or close a card, the way a click on its thumbnail does. */
function play(card) {
  card.querySelector(".video-thumb").dispatchEvent(new Event("click"));
}

function videoIn(card) {
  return card.querySelector(".player-host video");
}

/** A stand-in for hls.js, used only where the real one cannot run: jsdom has
 *  no MediaSource, so a real instance never gets past isSupported() and never
 *  reports any levels. The API surface here is the one app.js uses, and the
 *  test below checks the real library exports those same names. */
function installStubHls() {
  const handlers = {};
  const stub = {
    levels: [{ height: 720 }, { height: 480 }, { height: 360 }],
    currentLevel: -1,
    destroyed: false,
    on(event, handler) {
      handlers[event] = [...(handlers[event] ?? []), handler];
    },
    attachMedia(video) {
      this.media = video;
    },
    loadSource(url) {
      this.url = url;
    },
    destroy() {
      this.destroyed = true;
    },
    emit(event, data = {}) {
      for (const handler of handlers[event] ?? []) handler(event, data);
    },
  };
  class StubHls {
    constructor() {
      return stub;
    }
    static isSupported() {
      return true;
    }
    static get Events() {
      return { MANIFEST_PARSED: "hlsManifestParsed", ERROR: "hlsError" };
    }
  }
  window.Hls = StubHls;
  return {
    manifestParsed: () => stub.emit("hlsManifestParsed"),
    error: (data) => stub.emit("hlsError", data),
    instances: [stub],
  };
}

beforeEach(() => {
  vi.useFakeTimers();
});

afterEach(() => {
  vi.useRealTimers();
  vi.restoreAllMocks();
  delete window.Plyr;
  delete window.Hls;
});

describe("the page's player wiring", () => {
  it("points at vendored files that exist, with hls.js before Plyr", () => {
    const scripts = [...markup.matchAll(/<script src="([^"]+)"[^>]*><\/script>/g)].map((m) => m[1]);

    // Plyr has no HLS support of its own, so it has to be able to see hls.js,
    // and app.js has to see both.
    expect(scripts).toEqual(["vendor/hls.min.js", "vendor/plyr.min.js", "app.js"]);
    for (const src of scripts) {
      expect(existsSync(join(here, src)), `${src} is missing`).toBe(true);
    }

    expect(markup).toContain('<link rel="stylesheet" href="vendor/plyr.css" />');
    // The two files Plyr fetches for itself, plus the licences that have to
    // travel with the code. A vendored file that never got committed fails
    // here rather than as a missing icon in production.
    for (const asset of [
      "vendor/plyr.css",
      "vendor/plyr.svg",
      "vendor/blank.mp4",
      "vendor/plyr.LICENSE",
      "vendor/hls.LICENSE",
    ]) {
      expect(existsSync(join(here, asset)), `${asset} is missing`).toBe(true);
    }
  });

  it("keeps to the hls.js API the app is written against", () => {
    loadScript("vendor/hls.min.js");

    expect(typeof window.Hls.isSupported).toBe("function");
    // Both events the app listens for, by the names it listens for.
    expect(window.Hls.Events.MANIFEST_PARSED).toBe("hlsManifestParsed");
    expect(window.Hls.Events.ERROR).toBe("hlsError");

    const hls = new window.Hls();
    for (const method of ["on", "attachMedia", "loadSource", "destroy"]) {
      expect(typeof hls[method], `hls.${method}`).toBe("function");
    }
    expect(hls.levels).toEqual([]);
    expect(hls.currentLevel).toBe(-1); // hls.js's own "pick for me"
    hls.destroy();
  });
});

describe("playing a job", () => {
  it("plays a job's MP4 through the real Plyr, with no CDN in its config", async () => {
    const el = await loadPage();
    await openLibrary(el, [PLAIN]);
    const card = cardFor(el, "holiday.mp4");

    play(card);

    const video = videoIn(card);
    expect(card.querySelector(".plyr")).not.toBeNull();
    expect(video.getAttribute("src")).toBe(PLAIN.output_url);
    // Plyr takes the native controls away and draws its own instead -- the
    // visible difference between this and the bare <video> the app had before.
    expect(video.hasAttribute("controls")).toBe(false);
    expect(card.querySelector(".plyr__controls")).not.toBeNull();
    // Both of these default to cdn.plyr.io, and the sprite is fetched as the
    // player is built.
    expect(video.plyr.config.iconUrl).toBe("vendor/plyr.svg");
    expect(video.plyr.config.blankVideo).toBe("vendor/blank.mp4");
    // Pointing at the right path is not enough: the files have to be there.
    for (const asset of [video.plyr.config.iconUrl, video.plyr.config.blankVideo]) {
      expect(existsSync(join(here, asset)), `${asset} is missing`).toBe(true);
    }
  });

  it("takes the player apart again when the card is closed", async () => {
    const el = await loadPage();
    await openLibrary(el, [PLAIN]);
    const card = cardFor(el, "holiday.mp4");
    play(card);
    const video = videoIn(card);

    play(card);

    expect(card.querySelector(".plyr")).toBeNull();
    expect(card.querySelector("video")).toBeNull();
    expect(card.querySelector(".player-host")).toBeNull();
    // Plyr cancels the element's outstanding requests with its placeholder
    // video on the way out, which is the whole reason that file is vendored
    // too: the library's own default for it is a cdn.plyr.io URL.
    expect(video.getAttribute("src")).toBe("vendor/blank.mp4");
  });

  it("leaves no live player behind after ten toggles", async () => {
    const el = await loadPage();
    await openLibrary(el, [PLAIN]);
    const card = cardFor(el, "holiday.mp4");

    // Watch what the app builds, since a destroyed Plyr is invisible in the
    // DOM: it puts the element back the way it found it.
    const RealPlyr = window.Plyr;
    const created = [];
    window.Plyr = function Plyr(element, config) {
      const instance = new RealPlyr(element, config);
      created.push(instance);
      return instance;
    };

    for (let i = 0; i < 10; i += 1) {
      play(card);
      play(card);
    }

    expect(created).toHaveLength(10);
    // destroy() sets ready back to false. Anything still ready is still
    // holding listeners on an element nobody can see any more.
    expect(created.filter((player) => player.ready !== false)).toEqual([]);
    expect(card.querySelector(".plyr")).toBeNull();
    expect(el.libraryGrid.querySelectorAll("video")).toHaveLength(0);
  });

  it("puts the real Plyr on the upload card's finished job too", async () => {
    const el = await loadPage();
    fetchMock
      .mockResolvedValueOnce(jsonResponse({ job_id: "job-1" }))
      .mockResolvedValue(jsonResponse({ status: "done", output_url: PLAIN.output_url }));

    Object.defineProperty(el.fileInput, "files", {
      value: [new File(["data"], "holiday.mp4", { type: "video/mp4" })],
      configurable: true,
    });
    el.fileInput.dispatchEvent(new Event("change"));
    await vi.waitFor(() => expect(fetchMock).toHaveBeenCalled());
    await vi.advanceTimersByTimeAsync(0);

    expect(el.player.hidden).toBe(false);
    expect(el.player.querySelector(".plyr")).not.toBeNull();
    expect(el.player.querySelector("video").getAttribute("src")).toBe(PLAIN.output_url);
  });
});

describe("the adaptive ladder", () => {
  it("builds Plyr's quality menu from the ladder's own levels", async () => {
    const el = await loadPage();
    const hls = installStubHls();
    await openLibrary(el, [LADDER]);
    const card = cardFor(el, "vertical.mp4");

    play(card);
    expect(hls.instances[0].url).toBe(LADDER.hls_url);
    // Plyr builds its menu once, from the list it is handed, so it is created
    // after the manifest -- not before it, with levels still unknown.
    expect(card.querySelector(".plyr")).toBeNull();

    hls.manifestParsed();

    const video = videoIn(card);
    const menu = [...card.querySelectorAll('[role="menuitemradio"]')].map((b) =>
      b.textContent.trim(),
    );
    expect(menu.slice(0, 4)).toEqual(["Auto", "720pHD", "480pSD", "360p"]);

    video.plyr.quality = 480;
    expect(hls.instances[0].currentLevel).toBe(1);
    video.plyr.quality = 0;
    expect(hls.instances[0].currentLevel).toBe(-1);
  });

  it("hands the ladder straight to a browser that plays HLS itself", async () => {
    const el = await loadPage();
    // The one thing Safari does that the others do not. jsdom answers "" for
    // this, which is the fallback path the other tests cover.
    vi.spyOn(HTMLMediaElement.prototype, "canPlayType").mockReturnValue("probably");
    await openLibrary(el, [LADDER]);
    const card = cardFor(el, "vertical.mp4");

    play(card);

    const video = videoIn(card);
    expect(video.getAttribute("src")).toBe(LADDER.hls_url);
    expect(card.querySelector(".plyr")).not.toBeNull();
    // No level list is forced on it: with the browser doing the adapting, its
    // own controls carry the quality menu.
    expect(video.plyr.config.quality.forced).toBe(false);
  });

  it("falls back to the MP4 when nothing on the page can play the ladder", async () => {
    const el = await loadPage();
    loadScript("vendor/hls.min.js");
    // The real library's own answer under jsdom, which has no MediaSource --
    // and the same answer a browser without MSE gives.
    expect(window.Hls.isSupported()).toBe(false);
    await openLibrary(el, [LADDER]);
    const card = cardFor(el, "vertical.mp4");

    play(card);

    expect(videoIn(card).getAttribute("src")).toBe(LADDER.output_url);
  });

  it("sets no source at all when a ladder has no MP4 behind it", async () => {
    const el = await loadPage();
    const hls = installStubHls();
    const ladderOnly = { id: "3", filename: "only.m3u8.mp4", status: "done", hls_url: "/api/hls/3.m3u8" };
    await openLibrary(el, [ladderOnly]);
    const card = cardFor(el, "only.m3u8.mp4");

    play(card);

    // An empty src would have the element request the page itself, so there is
    // nothing to set -- and nothing to drop back to either.
    const video = videoIn(card);
    expect(video.hasAttribute("src")).toBe(false);

    hls.error({ fatal: true });
    await vi.advanceTimersByTimeAsync(0);
    expect(videoIn(card)).toBe(video);
  });

  it("drops back to the MP4 when the ladder fails, leaving the old menu inert", async () => {
    const el = await loadPage();
    const hls = installStubHls();
    await openLibrary(el, [LADDER]);
    const card = cardFor(el, "vertical.mp4");
    play(card);
    hls.manifestParsed();
    const staleMenu = videoIn(card).plyr.config.quality.onChange;

    hls.error({ fatal: true });
    await vi.advanceTimersByTimeAsync(0);

    expect(videoIn(card).getAttribute("src")).toBe(LADDER.output_url);
    expect(card.querySelector(".plyr")).not.toBeNull();
    expect(hls.instances[0].destroyed).toBe(true);
    // The rebuild throws the old player away, but its quality menu still
    // exists in whatever the browser kept alive -- picking from it must do
    // nothing rather than throw.
    expect(() => staleMenu(480)).not.toThrow();
  });

  it("does not rebuild a card that was closed while the ladder was failing", async () => {
    const el = await loadPage();
    const hls = installStubHls();
    await openLibrary(el, [LADDER]);
    const card = cardFor(el, "vertical.mp4");
    play(card);
    hls.manifestParsed();
    play(card);

    hls.error({ fatal: true });
    await vi.advanceTimersByTimeAsync(0);

    expect(card.querySelector(".player-host")).toBeNull();
    expect(el.libraryGrid.querySelectorAll("video")).toHaveLength(0);
  });
});

describe("the editing panel's player", () => {
  it("edits through the real Plyr, and takes it down on the way out", async () => {
    const el = await loadPage();
    await openLibrary(el, [PLAIN]);
    const card = cardFor(el, "holiday.mp4");

    card.querySelector(".card-edit").dispatchEvent(new Event("click"));

    expect(document.getElementById("view-edit").hidden).toBe(false);
    expect(document.getElementById("edit-source").textContent).toBe("holiday.mp4");
    const video = document.querySelector("#edit-player video");
    expect(document.querySelector("#edit-player .plyr")).not.toBeNull();
    expect(video.getAttribute("src")).toBe(PLAIN.output_url);

    document.getElementById("edit-back").dispatchEvent(new Event("click"));

    expect(document.querySelector("#edit-player .plyr")).toBeNull();
    expect(document.querySelector("#edit-player video")).toBeNull();
  });
});
