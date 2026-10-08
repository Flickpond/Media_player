# Login latency: why login p90 was 5.8 s at 50 users

**Status:** root cause reproduced locally on 8 October 2026, fix verified
locally. Staging numbers pending (§5). Track D, sprint 4.

## 1. Summary

`POST /auth/login` verifies the password with bcrypt (cost 12) **directly on
the API's event loop**. One verification takes about 220 ms of CPU, and for
that time the single uvicorn process cannot serve any other request. Under a
burst of logins the event loop is busy for longer than real time passes, so
logins queue behind each other, and **every other route queues behind them
too**.

Moving the two bcrypt calls to a worker thread removed the effect entirely:
login p90 1,448 → 243 ms, and an unrelated route's p90 770 → 8 ms during the
same burst. The fix is four lines in `app/api/auth.py`, which belongs to
another track (§4).

## 2. What the load test showed

From [`load-test-cloud-20260929.md`](load-test-cloud-20260929.md): Locust,
5 users/s ramp, each user logging in **once** at start, then polling a job.

| Users | Login p90 | Job read p99 | Job read max |
|---:|---:|---:|---:|
| 10 | 2,100 ms | 790 ms | |
| 25 | 2,900 ms | 1,700 ms | |
| 50 | 5,800 ms | 3,900 ms | ~8,100 ms |

Two things point at the event loop rather than at login alone. Login latency
grows with the number of users even though each logs in only once, which
is a queue. And the cheap job-read route has a long tail that grows the
same way, though it does no hashing: those reads were waiting behind logins
during the ramp. The API's CPU snapshots were taken after each stage, once
the logins were over, which is why they read ~0.1%.

## 3. Reproduction

[`load/login_burst.py`](../load/login_burst.py) starts logins at a fixed rate
(the Locust ramp's shape) while a probe calls `GET /api/health` every 100 ms.
`/health` does no bcrypt and no database work, so if it slows down, something
else is holding the process.

Local Compose stack, API container limited to **2 CPUs** like the current
server (`cpus: 2`), 50 logins:

| Run | Rate | Login p50 / p90 / max | `/health` during burst p50 / p90 / max | Probes completed |
|---|---:|---|---|---:|
| A. current code | 5/s | 797 / **1,448** / 1,795 ms | 328 / **770** / 1,074 ms | 25 |
| B. bcrypt in a thread | 5/s | 231 / **243** / 246 ms | 7 / **8** / 12 ms | 92 |
| C. bcrypt in a thread | 15/s | 1,980 / 2,733 / 2,877 ms | 11 / 95 / 100 ms | 45 |

`/health` with no logins running: 6 / 8 / 9 ms in every run.

One verification on this machine: **219 ms** for a real hash, **229 ms** for
the dummy hash used when the email does not exist (same cost, as intended).

- **A** reproduces the production shape: logins queue, and an unrelated
  route is slowed 50–100× while they do. At 5/s × 220 ms the loop needs 1.1 s
  of CPU per second; it falls behind and stays behind until the burst ends.
- **B** is the fix. Each login costs one hash and nothing else waits.
- **C** is the limit the fix does not remove: past what the cores can hash
  (2 cores ≈ 9 logins/s here), logins queue for CPU. The rest of the API
  stays responsive, because the queue is now in the thread pool, not in
  front of every request.

Production's 5.8 s is consistent with A on a slower core: at ~300 ms per
hash, 50 logins arriving over 10 s need ~15 s of loop time. That per-hash
figure is an estimate until measured (§5).

## 4. The fix, for the owner of `app/api/auth.py`

```python
from starlette.concurrency import run_in_threadpool

# login
if not await run_in_threadpool(verify_password, credentials.password, candidate) or user is None:

# register
password_hash=await run_in_threadpool(hash_password, credentials.password),
```

`bcrypt` releases the GIL while hashing, so threads really run in parallel
on separate cores. `run_in_threadpool` is what `app/api/jobs.py` and
`app/api/uploads.py` already use for blocking calls.

**Do not** fix this by:

- **lowering the cost factor.** That makes every stolen hash cheaper to
  crack, permanently, to buy back latency a thread pool gives for free;
- **skipping the dummy-hash verification for unknown emails.** It is what
  stops the response time from telling an attacker which addresses have
  accounts, and it costs the same as a real one (measured above);
- **adding uvicorn workers instead.** Two processes means two event loops
  that each still block, so the stall is halved, not removed, and
  each process holds its own database pool.

A test that would have caught it: two requests in flight at once, one
login and one `/health`, asserting `/health` returns before the login does.

## 5. Still to measure: staging and production per-hash time

Not done here: staging was stopped and this machine has no AWS credentials.

**Per-hash time**, read-only and about two seconds of CPU, safe on any host:

```bash
docker compose exec api python -c "import time; from app.services.security import hash_password, verify_password; h=hash_password('x'*12); t=time.perf_counter(); [verify_password('x'*12, h) for _ in range(5)]; print(round((time.perf_counter()-t)/5*1000), 'ms')"
```

**The burst**, staging only (the tool refuses production), with a test
account created for it:

```bash
LOGIN_BURST_CONFIRM=https://<staging-host> python load/login_burst.py \
  --target https://<staging-host> --email <test account> --password <…> \
  --logins 50 --rate 5
```

Record both here, for the current code and again after the fix ships.
Staging's 8 vCPU raise the CPU limit of run C roughly fourfold, but cannot
fix run A: one event loop is still one core.

## 6. Related, not done

- **No rate limit on login.** Each attempt costs ~220 ms of CPU, so a single
  client sending ~10 attempts per second saturates a 2-core host, fix or not
  (run C). An nginx `limit_req` zone on `/api/auth/` would bound it per
  client address. That is a decision for the team (it also slows password
  guessing), not part of this investigation.
