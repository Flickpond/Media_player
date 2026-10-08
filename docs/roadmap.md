# Roadmap

**Written:** end of sprint 3, 27 September 2026 · against `main` @ `b042c86`
**Rendered version:** <https://claude.ai/artifact/7EBv6ybhgN9fVy34ajiVuS>

Sprint 3 made video adaptive and editable, and last week's load test (#49) measured how it
holds up. Sprint 4 brings **4K**, a move to **AWS**, and a faster upload
pipeline. After that the work shifts to
**scaling out**: more workers when they're needed, and long videos split
across all of them.

This is a direction, not a plan. Each sprint still gets its own dated plan
with owners, acceptance criteria and effort, written at its planning session.

---

## Where we are (end of sprint 3)

### Live on flickpond.com

- Adaptive playback at 360p, 480p and 720p, switching with connection speed.
- Editing backend for all five operations: crop, clip, downscale, upscale, convert.
- Retry for failed videos, and validation that stops bad edits reaching FFmpeg.
- New player (Plyr) everywhere a video plays.
- SonarCloud and OWASP ZAP running in CI; all six checks green.

### Limits carried forward

- **720p is the maximum quality anywhere** — the MP4 and the HLS ladder are both capped.
- **Uploads are capped at 100 MB**, about 20 seconds of phone 4K.
- The crop box and clip scrubber have no user interface yet.
- Library cards have no thumbnails.
- Security scans report findings but **cannot block a merge** (`continue-on-error` on SonarCloud; ZAP accepts exit code 1).
- No Terraform.
- Load tested after sprint 3 (#49): reads meet the 2-second target at 50 users, but **login p90 is 5.8 s**.
- **A fresh clone cannot start the stack**: neither Docker Hub nor Quay serves the pinned MinIO image anonymously any more. Existing machines and production work because it is cached.

---

## Sprint 4 — 4K, AWS, and a faster pipeline

**Done when:** a phone's 4K video uploads with a progress bar, plays in 4K
when the connection allows, and the editor offers only the edits that make
sense for it.

### Hosting on AWS

| Piece | Choice | Reason |
|---|---|---|
| Region | `ap-southeast-1` (Singapore) | Closest to NUS; no ICP filing needed. |
| Server | One EC2 instance running the current Compose stack | Smallest change that makes 4K possible. Graviton (`c7g`) is cheaper per encode; `c7i` if anything turns out to be x86-only. |
| Video storage | S3, replacing MinIO | Fixes the dead MinIO image, and enables direct multipart uploads. |
| Disk | EBS `gp3`, 50 GB | OS, images and Postgres only. Videos live in S3. |
| Database, queue | Postgres and Redis stay in Compose | RDS and ElastiCache add cost without changing what we demonstrate. |
| Infrastructure | Terraform from day one | Describe the new setup as code instead of documenting a hand-built server. |

Size the instance by timing a real 4K clip through the full ladder in the
first week — resizing is a stop, a type change, and a start. Stop the
instance whenever nobody is using it; that is the largest saving available.

### The work

Owners, days and contracts are in [`sprint4-plan.md`](sprint4-plan.md); this
list is the scope.

- **★ The HLS ladder as its own job**, so a video is watchable as soon as the MP4 exists. Ladder up to 2160p, never taller than the source.
- Save the source's width, height and duration on the job.
- **★ Thumbnails**: one poster frame per video.
- **★ Direct multipart upload to storage**, resumable, with an upload progress bar.
- Source-aware edit rules, an immediate 422 for impossible scale requests, and limits by **duration** instead of megabytes. Probe first, and reject corrupt, too-long or video-less files in seconds.
- Edit from the **original upload** instead of the 720p copy — only together with the crop box.
- Edit options built from the video's real height; processing stages instead of a spinner.
- The AWS move with Terraform; security scans that can fail a merge.

★ marks the top three: together they remove the long-video timeout risk, lift
the size limit, start playback sooner, and make the Library look finished.

Encoding 720p once (FFmpeg `tee`) was on this list, but it conflicts with the
ladder running as its own job — separate jobs can't share an encode — so it
moved to sprint 5.

> **Two items must ship together.** Once edits start from the original
> upload, the crop box has to measure its rectangle in the original's pixels,
> not the 720p player's. Track B's change and Track E's crop box land in the
> same merge, or crops end up in the wrong place.

> **If the upload limit rises before direct uploads exist:** it is enforced in
> five places — `app/api/uploads.py`, `nginx.conf`, the frontend's size check
> and hint, the docs, **and the server's own `deploy/nginx-tls/tls.conf`, which
> is gitignored and never touched by a deploy**. The 900-second job timeout has
> to rise with it, or long videos fail outright instead of just losing their
> quality levels.

---

## Sprint 5 and later — scaling out

Sprint 4 keeps everything on one machine. These items spread the encoding
work across many, which is where the horizontal-scaling story for the course
becomes visible.

### Workers that scale with demand

The workers are already stateless: any worker can take any job (sprint 1,
N5). That makes them the part of the system that can scale. Postgres and
Redis hold state, so they stay on one always-on machine.

```text
                    ┌───────────────────────────────┐
  users ──────────▶ │ core instance, always on      │
                    │ nginx · API · Postgres · Redis│
                    │ reaper                        │
                    └───────────────┬───────────────┘
                                    │ private network only
                    ┌───────────────▼───────────────┐
                    │ worker Auto Scaling Group     │──▶ S3
                    │ spot instances, 0 … N         │
                    │ scaled by queue length        │
                    └───────────────────────────────┘
```

**Decided: a scheduled minimum.** With zero workers, the first upload after a
quiet period waits one to three minutes for an instance to boot. So the group
keeps a floor during the day and drops it at night:

| When | Workers |
|---|---|
| Working and demo hours | at least 1 |
| Overnight | 0 |
| Whenever the queue grows | more, up to the group's maximum |

What it requires:

- **Groundwork, prepared in sprint 4:** a queue-length metric, the worker image in ECR, and the autoscaling module written and `terraform plan`ned against the sprint 4 base. Sprint 5 applies it.
- **S3**, so workers on different machines share storage (done in sprint 4).
- **Postgres and Redis reachable on the private network**, only from the
  worker group's security group. This changes the sprint 1 rule that every
  datastore binds to `127.0.0.1`, so it needs its own write-up.
- **Worker images in a registry (ECR)**, because new instances pull an image
  at boot instead of building one.
- **Workers that disappear mid-job** — see the next section.

**Sizing and cost** (Singapore, on-demand and spot prices on 8 October 2026):

| Setup | Per month |
|---|---|
| One `c7g.2xlarge`, always on (sprint 4 staging) | ≈ $250 |
| One `c7g.xlarge`, always on | ≈ $125 |
| **Core `t4g.medium` always on + spot `c7g.xlarge` workers only while busy** | **≈ $40–65** |

The core runs nothing CPU-heavy, so a `t4g.medium` (2 vCPU, 4 GB, ≈ $31 a
month) holds it. A spot `c7g.xlarge` was ≈ $0.09 an hour, against $0.17 on
demand; two workers busy two hours a day cost ≈ $11 a month, and the daytime
floor of one worker adds ≈ $22. Video traffic out of S3 (≈ $0.12/GB) is on
top in every setup.

Smaller workers don't make one video faster — one video is one FFmpeg run on
one machine, so a 4K ladder on a 4-vCPU worker takes about twice as long as
on the 8-vCPU staging host (≈ 1 minute for 20 s of 4K instead of 28 s). More
workers means more videos encoding at once. Splitting one video across
machines is chunked encoding, below.

Until this lands, a sprint 4 move to AWS runs everything on **one
`c7g.xlarge`** (`instance_type` in `staging.tfvars`): half the cost of the
staging host, no code change, and 4K still well inside the timeout.

### Re-queue a job when its worker is told to stop (Track A)

A spot instance gets **two minutes' warning** before AWS takes it back, and
scale-in stops workers the same way. Today the job that worker was running
stays `processing` until the reaper's lease runs out — 40 minutes on AWS —
and is then marked **failed**. The uploader sees an error for something that
was never wrong with their video. With spot workers, that is routine, not
rare.

What changes:

- **The worker catches the stop signal** (`SIGTERM`, which Docker sends on
  shutdown and the instance's spot-interruption handling triggers), kills its
  FFmpeg, and puts the job back: `processing → queued` for an MP4 job, or the
  ladder back on its queue with `hls_status` still `pending`. A one-way
  transition rule is relaxed for exactly this case, through
  `app/repositories/jobs.py` like every other write, conditional on the job
  still being this worker's.
- **A bound on retries,** so a video that crashes FFmpeg every time cannot
  loop forever: an attempt count on the job, failed with a readable error
  after the third interruption.
- **The reaper stays the safety net** for a worker that dies without warning
  (a kernel panic, a reclaim that beats the signal). Its lease should drop
  from 40 minutes to a few minutes past the job timeout once workers
  heartbeat, which RQ already supports.
- **Partial output is discarded:** the job's `outputs/{id}/` prefix is cleared
  before the retry, so a half-written ladder is never served.

Acceptance: `docker stop` on a worker mid-encode puts the job back in the
queue within seconds, another worker finishes it, and the uploader never
sees `failed`. The same on a spot worker using AWS FIS's spot-interruption
test action.

### Chunked parallel encoding

How Netflix and YouTube encode an hour of video in minutes: split the source
into chunks of a few seconds, encode every chunk as its own job across all
workers at once, then join them with one final job. It fits our queue
directly, and it gives the autoscaling group a visible reason to scale out.

- Split at keyframes, so chunks join without re-encoding.
- The joining job waits until every chunk job has finished — RQ supports job dependencies.
- A failed chunk retries on its own, without restarting the whole video. This is also why it pairs well with cheap spot instances.
- Every chunk uses identical encoder settings, so no seams show where they join.

### Delivery at scale

- **CloudFront** in front of the segments, so viewers are served from nearby
  edge servers instead of our instance. (Its signed URLs work differently
  from S3's, so this is its own piece of work.)
- **Encoding by popularity.** Every upload gets a fast baseline; the
  expensive versions are made only for videos people actually watch.
- **AV1 for popular videos.** Slower to encode but smaller to stream — worth
  it when a video is watched many times.

---

## Considered, and not planned

| Option | Reason |
|---|---|
| AWS MediaConvert | It would handle transcoding for us, but it replaces the worker: the job system the whole project is built to demonstrate. |
| ECS or Kubernetes | The right tools at a larger scale; too much operational overhead for five people. |
| Per-title encoding analysis | Saves bandwidth at Netflix's volume. At ours it adds CPU cost for no visible gain. |
| Custom hardware or our own CDN | What YouTube and Netflix run. Not relevant at our size. |
