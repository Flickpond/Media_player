# Public production load observation, 29 September 2026

**Outcome:** The 50-user completed-job read routes had p90 below 2,000 ms
and no request failures. **The all-non-video-route criterion was not met:**
one-time login p90 was 5,800 ms at 50 users. A separate real-browser HLS
check decoded the first frame in 442.4 ms (<5,000 ms). These are production
observations, not a substitute for the proposal's staging acceptance test.

## Scope and controls

- Public HTTPS target: `https://flickpond.com`. Observed between 14:00 and
  14:14 UTC+8 in the approved 14:00-14:30 window. Local and deployed Git
  revision: `7b94fb958250cbdc434225594d8c66bb7e1ee413`.
- Locust 2.37.5 from one Windows load generator; one 15-second single-user
  smoke test, then separately attended 10, 25 and 50 VU stages, each 3 min
  with a 5 users/s ramp. Each VU logged in once and polled one completed job
  every ~2 s, listing jobs every tenth iteration and checking identity every
  twentieth. No uploads, edits or retries during read stages.
- One original 84,522-byte, two-second MP4 was uploaded once. Upload-to-202
  was 0.132 s and upload-to-`done` was 2.213 s; HLS was present. Login,
  `/api/auth/me`, and jobs-list smoke calls all returned 200 before upload.
  The 1 VU stage performed eight job reads and one login, with zero failures.
- The pre-stage, between-stage and final read-only server snapshots showed
  nine running containers (including two healthy workers), queue depth 0,
  and 11 GB free of 40 GB on `/` (71% used). Post-25 and post-50 API CPU
  sampled ~0.1%; one post-10 worker snapshot sampled 47% CPU. No sustained
  threshold violation can be established from these point-in-time samples;
  continuous server-side CPU, RAM and disk time series were **not saved**.

## Request results

Values are from the saved Locust `*_stats.csv` snapshots, including ramp-up.
Final console totals can differ by one request at shutdown. p90/p95/p99
are Locust's approximate millisecond percentiles.

| Users | Completed-job reads | Read p90/p95/p99 (ms) | List p90 (ms) | Me p90 (ms) | Login p90 (ms) | All requests | Failures | All req/s |
| ----: | ------------------: | --------------------: | ------------: | ----------: | -------------: | -----------: | -------: | --------: |
|    10 |                 900 |        70 / 100 / 790 |            73 |          54 |          2,100 |        1,040 |        0 |      5.80 |
|    25 |               2,202 |     330 / 550 / 1,700 |           400 |         140 |          2,900 |        2,527 |        0 |     14.08 |
|    50 |               4,221 |     660 / 700 / 3,900 |           340 |         110 |          5,800 |        4,871 |        0 |     27.21 |

At 50 VU the aggregate p90 was 650 ms, but that aggregate masks the slow
50 login requests. Their p90 exceeded 2 s at every stage and reached 5.8 s
at 50 VU; login was made only once per VU during ramp-up, not continuously.
The status route's maximum observed latency was about 8.1 s at 50 VU even
though its p90 was 660 ms. All failure CSVs were empty. These results cover
read-heavy polling of an already-completed job, not concurrent uploads or
concurrent transcoding capacity. The 50 VU production result **does not pass**
the strict per-route non-video p90 target because of login.

## Playback, comparison and cleanup

After the read stages, Edge measured HLS time from click to decoded video
frame at **442.4 ms**, with two HLS redirects, two successful object responses,
no object failures and no media error. The local September 25 run had no
first frame in 15 s and two failed object responses (403), so the public
playback result differs from that local result. Neither result proves staging
playback performance under simultaneous write load. No MP4-only control was
run for the cloud observation.

The [local report](load-test-local-20260925.md) recorded completed-job p90s
of 14 / 96 / 180 ms at 10 / 25 / 50 VU, versus public HTTPS p90s of
70 / 330 / 660 ms here. Local 50 VU aggregate rate was 28.22 req/s versus
27.21 req/s publicly. The earlier local test used an older code revision
(`a997e6077d64a7556b6ab0fc5291f94843c29de8` plus local load scripts),
and different network paths and service conditions also limit causal
comparisons. The test job was deleted via the owner API; a follow-up GET returned
404. No other account jobs were deleted. The queue remained 0 and all nine
containers were running after cleanup.

Raw `fixture.json`, `playback-hls.json`, 1/10/25/50-user Locust CSV, time
series and HTML reports are retained only in the ignored local directory
`load/results/cloud-c951ca54-97f3-400e-aa81-209c91a03efa/`. Raw reports
must be reviewed for private data before sharing. No password, session
cookie, signed URL, SSH key or account identifier is included here. Rotate
the test account credential because it was previously sent through chat.