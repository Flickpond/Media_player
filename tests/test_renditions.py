"""Extra AV1 renditions: Keep HDR, and encode by popularity."""

import contextlib
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

import fakeredis
import pytest

from app.queue import BUILD_RENDITION_TASK, enqueue_rendition, get_ladder_queue
from app.services.popularity import PLAYS_KEY, record_play
from app.worker import __main__ as entrypoint
from app.worker import after_ladder
from app.worker.hls import content_type_for
from app.worker.probe import SourceProbe
from app.worker.renditions import (
    RenditionBuilder,
    add_to_master,
    even_width,
    peak_bandwidth,
    plan_rendition,
    queue_keep_hdr,
    rendition_command,
    rungs_for,
)
from app.worker.tasks import master_lock

PQ = SourceProbe(3840, 2160, 120.0, hdr=True, color_transfer="smpte2084")
HLG = SourceProbe(3840, 2160, 120.0, hdr=True, color_transfer="arib-std-b67")
SDR = SourceProbe(1920, 1080, 120.0)
HDR_1080 = SourceProbe(1920, 1080, 120.0, hdr=True, color_transfer="smpte2084")

LADDER_MASTER = (
    "#EXTM3U\n#EXT-X-VERSION:3\n"
    '#EXT-X-MEDIA:TYPE=AUDIO,GROUP-ID="group_audio",NAME="audio_2",DEFAULT=YES,URI="v2/index.m3u8"\n'
    '#EXT-X-STREAM-INF:BANDWIDTH=4103100,RESOLUTION=1920x1080,CODECS="avc1.640028,mp4a.40.2",'
    'AUDIO="group_audio",FRAME-RATE=30.000\nv0/index.m3u8\n'
)


# --- what gets encoded ---------------------------------------------------------


def test_hdr_keeps_the_transfer_the_camera_recorded():
    pq, hlg = plan_rendition("hdr", PQ, max_fps=30), plan_rendition("hdr", HLG, max_fps=30)

    assert (pq.video_range, pq.codecs) == ("PQ", "av01.0.08M.10.0.110.09.16.09.0")
    assert (hlg.video_range, hlg.codecs) == ("HLG", "av01.0.08M.10.0.110.09.18.09.0")
    assert "format=yuv420p10le" in pq.filters
    assert pq.color_args[pq.color_args.index("-color_trc") + 1] == "smpte2084"
    assert not any("tonemap" in f for f in pq.filters), "HDR is kept, not tone-mapped"


def test_an_sdr_source_gets_no_hdr_rendition():
    assert plan_rendition("hdr", SDR, max_fps=30) is None
    assert (
        plan_rendition("hdr", SourceProbe(1, 1, 1.0, hdr=True, color_transfer="x"), max_fps=30)
        is None
    )


def test_the_popular_rendition_is_8_bit_standard_range_tone_mapped_from_hdr():
    from_hdr, from_sdr = (
        plan_rendition("av1", HDR_1080, max_fps=60),
        plan_rendition("av1", SDR, max_fps=30),
    )

    assert from_hdr.video_range == from_sdr.video_range == "SDR"
    assert from_hdr.codecs == "av01.0.08M.08"
    assert any("tonemap" in f for f in from_hdr.filters)
    assert not any("tonemap" in f for f in from_sdr.filters)
    assert "min(60" in from_hdr.filters[0]


def test_a_source_above_1080p_gets_no_popular_rendition():
    """Its H.264 ladder goes to 1440p or 2160p; AV1 here stops at 1080p, and
    hls.js would hold a 4K screen there to save bytes."""
    assert plan_rendition("av1", PQ, max_fps=30) is None
    assert plan_rendition("hdr", PQ, max_fps=30) is not None, "HDR is worth the trade"


def test_every_rung_is_capped_under_the_h264_ceiling_at_its_height():
    """hls.js skips a codec family whose cheapest entry costs more than its
    first estimate -- about the cheapest entry in the playlist."""
    for kind, probe in (("av1", SDR), ("hdr", PQ)):
        for height, h264 in ((1080, 5_000_000), (720, 2_800_000), (360, 800_000)):
            plan = plan_rendition(kind, probe, max_fps=30, height=height)
            assert 0 < plan.maxrate < h264, (kind, height)


def test_the_bottom_rung_fits_hls_js_first_estimate_whatever_the_footage():
    """hls.js 1.7 starts in a family only if its cheapest entry fits
    max(cheapest anywhere, 500 kbps). Simple footage can bring H.264 360p far
    under 500 kbps, so the bottom rung must fit 500 kbps on its own: worst
    measured peak 1.25x the cap, plus the 128 kbps add_to_master adds for audio."""
    for kind, probe in (("av1", SDR), ("hdr", PQ)):
        for height in (360, 240):
            plan = plan_rendition(kind, probe, max_fps=30, height=height)
            assert plan.maxrate * 1.25 + 128_000 <= 500_000, (kind, height)


def test_unknown_kinds_are_refused():
    with pytest.raises(ValueError):
        plan_rendition("vp9", SDR, max_fps=30)


def test_the_command_is_av1_fmp4_hls_capped_at_1080p(tmp_path):
    command = rendition_command(
        plan_rendition("hdr", PQ, max_fps=30),
        source_path=Path("/s"),
        out_dir=tmp_path,
        ffmpeg_binary="ffmpeg",
        max_fps=30,
    )

    assert command[command.index("-c:v") + 1] == "libsvtav1"
    assert command[command.index("-hls_segment_type") + 1] == "fmp4"
    assert command[command.index("-hls_fmp4_init_filename") + 1] == "init.mp4"
    assert command[command.index("-maxrate") + 1] == "3750k"
    assert command[command.index("-bufsize") + 1] == "3750k"
    assert "-an" in command, "the ladder's audio group is shared"
    assert "min(1080" in command[command.index("-vf") + 1]
    assert command[-1].endswith("index.m3u8")


# --- the master playlist ---------------------------------------------------------


def test_the_rendition_is_listed_with_the_ladders_audio_and_frame_rate():
    plan = plan_rendition("hdr", PQ, max_fps=30)

    master = add_to_master(
        LADDER_MASTER,
        plan=plan,
        rung=1080,
        resolution="1920x1080",
        bandwidth=(3_000_000, 2_000_000),
    )

    entry = [line for line in master.splitlines() if "VIDEO-RANGE=PQ" in line][0]
    assert 'CODECS="av01.0.08M.10.0.110.09.16.09.0,mp4a.40.2"' in entry
    assert 'AUDIO="group_audio"' in entry and "FRAME-RATE=30.000" in entry
    assert "BANDWIDTH=3128000" in entry and "AVERAGE-BANDWIDTH=2128000" in entry
    assert "hdr/1080/index.m3u8" in master.splitlines()
    assert "v0/index.m3u8" in master.splitlines(), "the ladder is still there"


def test_renditions_go_ahead_of_the_ladder_hdr_first_whatever_the_order_built():
    """hls.js starts in the family of the first entry it can play (Chromium,
    hls.js 1.7): an SDR screen skips HDR and lands in AV1; no AV1, the ladder."""
    av1 = plan_rendition("av1", SDR, max_fps=30)
    hdr = plan_rendition("hdr", PQ, max_fps=30)
    master = add_to_master(
        LADDER_MASTER, plan=av1, rung=1080, resolution="1920x1080", bandwidth=(1, 1)
    )
    master = add_to_master(master, plan=av1, rung=360, resolution="640x360", bandwidth=(1, 1))
    master = add_to_master(master, plan=hdr, rung=1080, resolution="1920x1080", bandwidth=(1, 1))

    uris = [line for line in master.splitlines() if line and not line.startswith("#")]
    assert uris == [
        "hdr/1080/index.m3u8",
        "av1/1080/index.m3u8",
        "av1/360/index.m3u8",
        "v0/index.m3u8",
    ]
    assert master.splitlines()[:3] == LADDER_MASTER.splitlines()[:3], "the head is kept"


def test_each_av1_size_declares_its_own_level():
    assert plan_rendition("av1", SDR, max_fps=30, height=360).codecs == "av01.0.04M.08"
    assert plan_rendition("av1", SDR, max_fps=30, height=720).codecs == "av01.0.05M.08"
    assert plan_rendition("av1", SDR, max_fps=30, height=1080).codecs == "av01.0.08M.08"
    assert rungs_for("av1", 1080) == [1080, 720, 360]
    assert rungs_for("av1", 480) == [360]
    assert rungs_for("av1", 240) == [240]
    assert rungs_for("hdr", 2160) == [1080, 720, 360]


def test_listing_a_rendition_twice_changes_nothing():
    plan = plan_rendition("av1", SDR, max_fps=30)
    once = add_to_master(
        LADDER_MASTER, plan=plan, rung=1080, resolution="1920x1080", bandwidth=(1, 1)
    )

    assert (
        add_to_master(once, plan=plan, rung=1080, resolution="1920x1080", bandwidth=(1, 1)) == once
    )


def test_bandwidth_is_measured_from_the_segments(tmp_path):
    (tmp_path / "seg00001.m4s").write_bytes(b"x" * 1000)
    (tmp_path / "seg00002.m4s").write_bytes(b"x" * 3000)
    playlist = "#EXTINF:4.0,\nseg00001.m4s\n#EXTINF:2.0,\nseg00002.m4s\n"

    assert peak_bandwidth(playlist, tmp_path) == (12000, 5333)
    assert even_width(3840, 2160, 1080) == 1920
    assert even_width(1080, 1920, 1080) == 608


def test_fmp4_parts_are_served_as_what_they_are():
    assert content_type_for("init.mp4") == "video/mp4"
    assert content_type_for("seg00001.m4s") == "video/iso.segment"
    assert content_type_for("index.m3u8") == "application/vnd.apple.mpegurl"
    assert content_type_for("seg00001.ts") == "video/mp2t"


# --- building one ----------------------------------------------------------------


class Store:
    def __init__(self):
        self.objects = {
            "outputs/j/hls/master.m3u8": LADDER_MASTER.encode(),
            "uploads/j/s.mov": b"s",
        }
        self.uploads: list[str] = []

    def download_file(self, *, key, destination):
        Path(destination).write_bytes(self.objects[key])

    def upload_file(self, *, key, source, content_type="video/mp4"):
        self.uploads.append(key)
        self.objects[key] = Path(source).read_bytes()


def fake_av1(command, **_kwargs):
    out = Path(command[-1]).parent
    (out / "init.mp4").write_bytes(b"init")
    (out / "seg00001.m4s").write_bytes(b"x" * 2000)
    (out / "index.m3u8").write_text("#EXTM3U\n#EXTINF:4.0,\nseg00001.m4s\n#EXT-X-ENDLIST\n")
    return SimpleNamespace(returncode=0, stderr="")


def builder(store, probe, runner=fake_av1):
    return RenditionBuilder(store, prober=lambda _p: probe, runner=runner)


def run(b, kind="hdr"):
    return b.run(
        kind=kind, source_key="uploads/j/s.mov", hls_key="outputs/j/hls/master.m3u8", max_fps=30
    )


def test_the_master_is_uploaded_last_so_no_player_meets_a_half_rendition():
    store = Store()

    assert run(builder(store, PQ)) is True

    assert store.uploads[-1] == "outputs/j/hls/master.m3u8"
    assert set(store.uploads[:-1]) == {
        f"outputs/j/hls/hdr/{rung}/{name}"
        for rung in (1080, 720, 360)
        for name in ("init.mp4", "seg00001.m4s", "index.m3u8")
    }
    assert b"VIDEO-RANGE=PQ" in store.objects["outputs/j/hls/master.m3u8"]


def test_the_popular_rendition_is_built_at_three_sizes_listed_once():
    store = Store()

    assert run(builder(store, HDR_1080), kind="av1") is True

    master = store.objects["outputs/j/hls/master.m3u8"].decode()
    for rung in (1080, 720, 360):
        assert f"av1/{rung}/index.m3u8" in master
    assert store.uploads.count("outputs/j/hls/master.m3u8") == 1


def test_a_4k_source_asked_for_av1_is_left_alone():
    store = Store()

    assert run(builder(store, PQ, runner=lambda *a, **k: pytest.fail("encoded")), "av1") is False
    assert store.uploads == []


def test_an_sdr_source_asked_for_hdr_is_left_alone():
    store = Store()

    def never(*_a, **_k):
        raise AssertionError("no encode for an SDR source")

    assert run(builder(store, SDR, runner=never)) is False
    assert store.uploads == []


def test_a_rendition_already_listed_is_not_built_again():
    store = Store()
    run(builder(store, PQ))
    store.uploads.clear()

    assert run(builder(store, PQ, runner=lambda *a, **k: pytest.fail("encoded twice"))) is False
    assert store.uploads == []


def test_a_failed_encode_leaves_the_master_as_it_was():
    store = Store()

    def broken(*_a, **_k):
        return SimpleNamespace(returncode=1, stderr="svt-av1 failed")

    with pytest.raises(Exception, match="hdr rendition"):
        run(builder(store, PQ, runner=broken))
    assert store.objects["outputs/j/hls/master.m3u8"] == LADDER_MASTER.encode()


# --- when they are queued ---------------------------------------------------------


def test_a_play_that_reaches_the_threshold_queues_av1_once():
    redis, queued, job = fakeredis.FakeRedis(), [], uuid4()

    results = [
        record_play(redis, job, threshold=3, enqueue=lambda j, k: queued.append((j, k)))
        for _ in range(5)
    ]

    assert results == [False, False, True, False, False]
    assert queued == [(job, "av1")]
    assert int(redis.get(PLAYS_KEY.format(job))) == 5


def test_no_threshold_means_no_counting_and_redis_trouble_costs_only_the_count():
    redis = fakeredis.FakeRedis()
    assert record_play(redis, uuid4(), threshold=0, enqueue=lambda *a: None) is False
    assert redis.keys() == []

    class Broken:
        def incr(self, _key):
            raise ConnectionError("down")

    assert record_play(Broken(), uuid4(), threshold=3, enqueue=lambda *a: None) is False


def test_renditions_are_queued_on_the_ladder_queue_one_per_kind():
    redis = fakeredis.FakeRedis()
    job = uuid4()

    enqueue_rendition(job, "hdr", queue=get_ladder_queue(redis))

    queued = get_ladder_queue(redis).jobs
    assert [(j.func_name, j.args, j.id) for j in queued] == [
        (BUILD_RENDITION_TASK, (str(job), "hdr"), f"{job}-hdr")
    ]


def test_keep_hdr_registers_its_hook_only_when_switched_on(monkeypatch):
    hooks: list = []
    monkeypatch.setattr(after_ladder, "HOOKS", hooks)

    entrypoint.register_hooks(SimpleNamespace(worker_keep_hdr=False, worker_preview_sprite=False))
    assert hooks == []
    entrypoint.register_hooks(SimpleNamespace(worker_keep_hdr=True, worker_preview_sprite=False))
    assert hooks == [queue_keep_hdr]


def test_the_keep_hdr_hook_queues_the_hdr_rendition(monkeypatch):
    queued = []
    monkeypatch.setattr("app.queue.enqueue_rendition", lambda job, kind: queued.append((job, kind)))

    queue_keep_hdr("job-1", "outputs/job-1/x.mp4")

    assert queued == [("job-1", "hdr")]


def test_a_rendition_kind_from_the_queue_must_be_one_this_module_builds():
    with pytest.raises(ValueError, match="unknown rendition"):
        builder(Store(), PQ).run(
            kind="../../etc", source_key="k", hls_key="outputs/j/hls/master.m3u8", max_fps=30
        )


# --- two renditions at once ------------------------------------------------------


def test_a_rendition_listed_while_another_encoded_is_kept():
    """On flickpond.com the AV1 job uploaded the master it had read before the
    HDR job finished, and the three HDR entries were gone."""
    store = Store()

    def hdr_lands_meanwhile(command, **kwargs):
        if not store.uploads:
            run(builder(store, PQ))
        return fake_av1(command, **kwargs)

    assert run(builder(store, HDR_1080, runner=hdr_lands_meanwhile), kind="av1") is True

    master = store.objects["outputs/j/hls/master.m3u8"].decode()
    for kind in ("hdr", "av1"):
        for rung in (1080, 720, 360):
            assert f"{kind}/{rung}/index.m3u8" in master


def test_the_master_is_read_and_written_inside_the_lock():
    store, events = Store(), []

    @contextlib.contextmanager
    def lock(hls_key):
        events.append(("lock", hls_key))
        yield
        events.append(("unlock", hls_key))

    original = store.upload_file

    def upload(*, key, **kwargs):
        if key.endswith("master.m3u8"):
            events.append(("master", key))
        original(key=key, **kwargs)

    store.upload_file = upload
    RenditionBuilder(store, prober=lambda _p: PQ, runner=fake_av1, master_lock=lock).run(
        kind="hdr", source_key="uploads/j/s.mov", hls_key="outputs/j/hls/master.m3u8", max_fps=30
    )

    key = "outputs/j/hls/master.m3u8"
    assert events == [("lock", key), ("master", key), ("unlock", key)]


def test_the_master_lock_is_one_per_video_and_exclusive():
    redis = fakeredis.FakeRedis()
    first = master_lock("outputs/a/hls/master.m3u8", connection=redis)
    again = master_lock("outputs/a/hls/master.m3u8", connection=redis)
    other = master_lock("outputs/b/hls/master.m3u8", connection=redis)

    assert first.acquire(blocking=False)
    assert not again.acquire(blocking=False)
    assert other.acquire(blocking=False)
    assert redis.ttl("flickpond:master-lock:outputs/a/hls/master.m3u8") <= 60, "expires alone"
