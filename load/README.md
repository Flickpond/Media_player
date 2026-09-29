# Load validation

By default this workload is restricted to `http://127.0.0.1:13000`. The
explicit production mode below is restricted to `https://flickpond.com` and
requires an approved, monitored maintenance window. It creates one small,
real video job, then exercises
authenticated reads from 10, 25 and 50 virtual users. The video is only a
fixture; the read load does not submit or transcode 50 videos. All raw results
under `load/results/` stay on the local machine and are ignored by Git.

Install on Python 3.12 or 3.13 (Windows uses installed Microsoft Edge for the
browser check):

```powershell
.\.venv\Scripts\python.exe -m pip install -r load/requirements.txt
```

Choose a new run directory, such as `load/results/local-001`. From the repo
root, set *all* of these in one PowerShell session before Compose; the explicit
internal DSN and endpoints prevent a deployment `.env` from redirecting the
test stack to cloud data:

```powershell
$env:COMPOSE_PROJECT_NAME = 'flickpond-load-local'
$env:COMPOSE_PROFILES = ''
$env:JWT_SECRET = 'local-load-only-jwt-secret-2026-change-me'
$env:POSTGRES_DB = 'flickpond'
$env:POSTGRES_USER = 'flickpond'
$env:POSTGRES_PASSWORD = 'local-load-only-postgres'
$env:POSTGRES_DSN = 'postgresql://flickpond:local-load-only-postgres@postgres:5432/flickpond'
$env:REDIS_HOST = 'redis'
$env:REDIS_PORT = '6379'
$env:REDIS_SSL = 'false'
$env:REDIS_PASSWORD = 'local-load-only-redis'
$env:MINIO_ENDPOINT = 'minio:9000'
$env:MINIO_USE_SSL = 'false'
$env:MINIO_PUBLIC_USE_SSL = 'false'
$env:MINIO_ROOT_USER = 'local-load-minio'
$env:MINIO_ROOT_PASSWORD = 'local-load-minio-password'
$env:MINIO_ACCESS_KEY = $env:MINIO_ROOT_USER
$env:MINIO_SECRET_KEY = $env:MINIO_ROOT_PASSWORD
$env:MINIO_PUBLIC_ENDPOINT = '127.0.0.1:13000'
$env:FRONTEND_BIND = '127.0.0.1'
$env:FRONTEND_PORT = '13000'
$env:FRONTEND_TLS_PORT = '13443'
$env:API_PORT = '18000'
$env:POSTGRES_PORT = '15432'
$env:REDIS_PUBLIC_PORT = '16379'
$env:MINIO_API_PORT = '19000'
$env:MINIO_CONSOLE_PORT = '19001'
$env:LOAD_BASE_URL = 'http://127.0.0.1:13000'
$env:LOAD_EMAIL = 'load-local-001@example.com'
$env:LOAD_PASSWORD = 'local-load-password-2026'
$compose = @('-f', 'docker-compose.yml', '-f', 'deploy/dast/compose.yml', '-f', 'load/compose.yml')
docker compose @compose config | Out-Null
docker compose @compose up --build -d --wait --scale worker=2 frontend reaper
```

Inspect the merged configuration and confirm every published port is on
`127.0.0.1`, the internal DSN points to `postgres:5432`, and MinIO uses
`flickpond-minio:load`. Do not proceed if any address is external.

Generate an original, two-second MP4 inside the API container, copy it into
the ignored run directory, then upload and wait for the worker:

```powershell
New-Item -ItemType Directory -Force load/results/local-001 | Out-Null
docker exec flickpond-load-local-api-1 ffmpeg -hide_banner -loglevel error -f lavfi -i testsrc2=size=320x180:rate=15 -f lavfi -i sine=frequency=440:sample_rate=16000 -t 2 -c:v libx264 -pix_fmt yuv420p -preset ultrafast -b:v 200k -c:a aac -b:a 32k -movflags +faststart -y /tmp/load-fixture.mp4
docker cp flickpond-load-local-api-1:/tmp/load-fixture.mp4 load/results/local-001/fixture.mp4
.\.venv\Scripts\python.exe load/fixture.py prepare --video load/results/local-001/fixture.mp4 --evidence load/results/local-001/fixture.json
$env:LOAD_JOB_ID = (Get-Content load/results/local-001/fixture.json -Raw | ConvertFrom-Json).job_id
```

Each phase lasts three minutes. Run them separately and inspect the previous
`*_stats.csv` and `*_failures.csv` before increasing concurrency:

```powershell
foreach ($users in 10, 25, 50) {
    .\.venv\Scripts\python.exe -m locust -f load/locustfile.py --headless --host $env:LOAD_BASE_URL --users $users --spawn-rate 5 --run-time 3m --csv "load/results/local-001/users-$users" --html "load/results/local-001/users-$users.html" --only-summary --exit-code-on-error 2
    if ($LASTEXITCODE -ne 0) { break }
}
```

The optional playback check uses an installed Edge, requests the actual UI
player and measures its first decoded frame. A failure is evidence, not a pass;
its JSON report does not contain signed object URLs:

```powershell
.\.venv\Scripts\python.exe load/playback.py --evidence load/results/local-001/fixture.json
```

When finished, delete the job through the API before removing the disposable
Compose project. Only this project's volumes are removed, not the existing
development or deployment volumes:

```powershell
.\.venv\Scripts\python.exe load/fixture.py cleanup --evidence load/results/local-001/fixture.json
docker compose @compose down -v --remove-orphans
```

At 50 users, compare each non-video route's p90 against 2,000 ms. Report
upload-to-202, processing-to-terminal, and browser first-frame separately;
the 50-user workload is local and does not establish production capacity.

## Production read test (operator attended)

Do not run this section until the owner approves a specific maintenance
window, the on-call observer is watching real users and containers, and the
cloud baseline shows healthy nginx, API, two workers, reaper, PostgreSQL,
Redis, MinIO, queue depth, CPU, RAM, and disk. Do not infer cloud health from
local Docker. Save redacted baseline snapshots before and after *each* stage.
Use one load generator; do not run distributed Locust or multiple instances.
The production account must already exist; this mode never registers one.
Only this run's fixture job may be removed. Raw HTML and CSV stay in the
Git-ignored results directory; review them for sensitive data before sharing.

In a PowerShell terminal on the load generator, from the repo root:

```powershell
$env:LOAD_MODE = 'production'
$env:LOAD_BASE_URL = 'https://flickpond.com'
$env:LOAD_RUN_ID = [guid]::NewGuid().ToString()
$env:LOAD_CONFIRM = "flickpond-production:$env:LOAD_RUN_ID"
$env:LOAD_EMAIL = Read-Host 'Approved test account email'
$env:LOAD_PASSWORD = [System.Net.NetworkCredential]::new('', (Read-Host -AsSecureString 'Test password')).Password
$runDir = "load/results/cloud-$env:LOAD_RUN_ID"
$env:LOAD_ALLOW_UPLOAD = '1'
.\.venv\Scripts\python.exe load/fixture.py prepare --video load/results/20260925-local/fixture.mp4 --evidence "$runDir/fixture.json"
Remove-Item Env:LOAD_ALLOW_UPLOAD
$env:LOAD_JOB_ID = (Get-Content "$runDir/fixture.json" -Raw | ConvertFrom-Json).job_id
```

The original MP4 is a two-second test pattern (under 1 MB). If it is not
available, create another original MP4 and check its size before the one
permitted upload. An interrupted prepare may leave a job ID in `fixture.json`;
inspect that record and clean up the same job rather than retrying upload.
No read stage should start unless this job reached `done`.

First run a 1-user, 15-second smoke test, then inspect its failures and the
cloud baseline. For each subsequent stage, set `$users` separately to 10,
then 25, then 50, running the command once per value. The next stage requires
the previous stage's CSV and live health to be reviewed by the observer; 50
users requires explicit approval in the active maintenance window.

```powershell
$users = 1
$duration = '15s'
.\.venv\Scripts\python.exe -m locust -f load/locustfile.py --headless --host $env:LOAD_BASE_URL --users $users --spawn-rate 5 --run-time $duration --csv "$runDir/users-$users" --html "$runDir/users-$users.html" --only-summary --exit-code-on-error 2
Import-Csv "$runDir/users-$users`_stats.csv" | Select-Object Type,Name,'Request Count','Failure Count','90%','Requests/s'
```

For each approved 10/25/50 stage use `$duration = '3m'` and set `$users` by
hand before running the same two lines. Abort immediately on persistent 5xx,
30 seconds above 1% errors or 2 s non-video p90, degraded health, CPU above
85% for 60 seconds, memory above 85%, disk free below 20%, a queue growing
by more than 10 for 30 seconds, or real-user impact. Do not automatically
continue after a failure. Capture each stage's redacted container and queue
snapshots on the server; no direct datastore access or deployment commands.

Outside the read phases, inspect HLS first-frame separately (a local test
previously found a 403 on the variant playlist). Whether it succeeds or not,
clean up the exact fixture job and verify 404:

```powershell
.\.venv\Scripts\python.exe load/playback.py --evidence "$runDir/fixture.json"
.\.venv\Scripts\python.exe load/fixture.py cleanup --evidence "$runDir/fixture.json"
Remove-Item Env:LOAD_PASSWORD,Env:LOAD_CONFIRM,Env:LOAD_ALLOW_UPLOAD -ErrorAction SilentlyContinue
```

If playback fails, still run cleanup. Report the 50-user non-video p90 and
first-frame <5 s criteria independently; a production observation does not
replace the proposal's staging acceptance test. Rotate test credentials
afterwards, especially if they were shared through a chat channel.