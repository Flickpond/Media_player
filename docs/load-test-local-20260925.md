# Local load validation, 25 September 2026

**Outcome:** authenticated API reads passed the local 50-user threshold. HLS
playback did **not** meet the 5-second first-frame target: the variant playlist
was requested without a signature and returned 403. No production traffic was
generated. This is a local proxy for the staging requirement in
[`proposal.md`](proposal.md); it is not staging or production evidence.

## Setup and reproducibility

- Git commit: `a997e6077d64a7556b6ab0fc5291f94843c29de8` plus the local
  load-test files in this change. Windows host: 24 logical processors,
  34,066,755,584 bytes RAM; Docker Desktop: 24 CPUs, 16,622,182,400 bytes RAM.
- Separate Compose project `flickpond-load-local` with its own PostgreSQL,
  Redis, source-built MinIO, nginx, API, reaper and **2 workers**. All host
  ports bound to `127.0.0.1`; no existing `media_player` container or volume
  was modified. MinIO release: `RELEASE.2024-06-13T22-53-53Z`.
- One dedicated user owned one 84,522-byte FFmpeg-generated, two-second MP4.
  Upload-to-202: **31 ms**; upload-to-`done`: **2.043 s**; HLS was produced.
  All 50 virtual users polled this same completed job every ~2 seconds.
- Locust 2.37.5, one local load generator, 5 users/s ramp-up, 3 minutes per
  level. One login per user; every tenth iteration also listed jobs and every
  twentieth checked identity. No upload was made during the read phases.

## Read results

Numbers below are from saved Locust `*_stats.csv` snapshots, including the
ramp-up period. Final console summaries can contain up to one more iteration
per user because CSV and runner shutdown are not simultaneous. Latencies are
Locust's approximate millisecond percentiles.

| Users | Completed-job reads | Read req/s | Read p90 / p95 / p99 | Jobs-page p90 | Auth-me p90 | All requests | Failures |
| ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| 10 | 900 | 5.03 | 14 / 15 / 240 ms | 15 ms | 8 ms | 1,040 | 0 |
| 25 | 2,220 | 12.46 | 96 / 100 / 150 ms | 95 ms | 29 ms | 2,550 | 0 |
| 50 | 4,375 | 24.54 | 180 / 200 / 890 ms | 190 ms | 61 ms | 5,030 | 0 |

At 50 users, the aggregate request rate was **28.22/s** and the aggregate
p90 was **180 ms** (p95 **200 ms**, p99 **940 ms**, including 50 login
requests). The 2,000 ms p90 criterion for non-video requests was met in this
configuration. The Redis queue was empty after the 25- and 50-user phases;
all eight test containers were healthy. This result covers read-heavy polling
of a *completed* job. It does not establish capacity for 50 simultaneous
uploads, 50 simultaneous FFmpeg jobs or production network latency.

## Playback finding and cleanup

The first browser attempt used the direct MinIO port and hit cross-origin
constraints. After correcting `MINIO_PUBLIC_ENDPOINT` to the same-origin
nginx port `127.0.0.1:13000`, an Edge browser still decoded **no first frame
within 15 s** (`MEDIA_ERR_SRC_NOT_SUPPORTED`, code 4). Edge supports the
output's H.264/AAC codecs. The signed HLS master returned 307 then 206;
subsequent requests for `hls/v0/index.m3u8` reached `/videos/` directly
without a signature and returned **403**. The HLS manifest's relative child
path was resolved against its *redirected* object URL, bypassing the API
route that signs individual HLS parts. This needs coordination with the HLS
API/frontend owners; the load test made no product-code changes. The MP4-only
browser control was not measured reliably due to Edge navigation timeouts and
must not be claimed as passed.

The dedicated job was deleted through the owner API and a subsequent GET
returned 404. The disposable Compose project, three volumes and network were
removed. The original development project remained running.

## Evidence files

Raw evidence is saved locally under `load/results/20260925-local/` (ignored
by Git because it may contain environment-dependent details). In particular:

- `fixture.json`: upload size, timings, terminal status, HLS presence and
  `cleanup_verified: true`.
- `users-10_stats.csv`, `users-25_stats.csv`, `users-50_stats.csv`: per-route
  requests and latency percentiles; corresponding `_stats_history.csv`,
  `_failures.csv`, `_exceptions.csv` and `.html` reports include time series.
- `resources-after-10.jsonl`, `resources-after-25.jsonl`,
  `resources-after-50.jsonl`: eight-container CPU/memory snapshots.
- `playback-hls.json`: no first frame, media error 4, two HLS redirects and
  two failed object requests; `worker-1.log`, `worker-2.log` and
  `containers-before-cleanup.txt` record worker activity and service health.

No cookies, account passwords or signed object URLs are included in this
tracked report. See [`load/README.md`](../load/README.md) to repeat the test.