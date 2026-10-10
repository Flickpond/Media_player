"""Login burst: does logging in slow down everything else?

Fires logins at a fixed rate (the shape of a Locust ramp, where every user
logs in once as it starts) while a probe calls a cheap API route every
100 ms, then reports both latency distributions.

If the probe slows down only while logins are in flight, something in the
login path holds the API's event loop: no other request on that process can
make progress until it lets go.

    python load/login_burst.py --email EMAIL --password PASSWORD
    python load/login_burst.py --target https://staging.example \\
        --email EMAIL --password PASSWORD   # needs LOGIN_BURST_CONFIRM, below

Local targets only by default. Anything else needs LOGIN_BURST_CONFIRM set to
the exact target URL, and production is refused outright: this sends
`--logins` real bcrypt verifications as fast as `--rate` says.
"""

import argparse
import asyncio
import json
import os
import statistics
import sys
import time
from urllib.parse import urlsplit

import httpx

PRODUCTION_HOSTS = {"flickpond.com", "www.flickpond.com"}
LOCAL_HOSTS = {"127.0.0.1", "localhost"}


def check_target(target: str) -> None:
    host = urlsplit(target).hostname or ""
    if host in PRODUCTION_HOSTS:
        raise SystemExit("refusing production; run this against local or staging")
    if host not in LOCAL_HOSTS and os.environ.get("LOGIN_BURST_CONFIRM") != target:
        raise SystemExit(f"non-local target: set LOGIN_BURST_CONFIRM={target} to confirm")


def percentile(values: list[float], fraction: float) -> float:
    ordered = sorted(values)
    return ordered[min(len(ordered) - 1, int(fraction * len(ordered)))]


def summary(values: list[float]) -> dict[str, float | int]:
    if not values:
        return {"n": 0}
    return {
        "n": len(values),
        "p50_ms": round(statistics.median(values) * 1000),
        "p90_ms": round(percentile(values, 0.90) * 1000),
        "max_ms": round(max(values) * 1000),
    }


async def login(client: httpx.AsyncClient, email: str, password: str) -> tuple[float, int]:
    started = time.perf_counter()
    response = await client.post("/api/auth/login", json={"email": email, "password": password})
    return time.perf_counter() - started, response.status_code


async def probe(client: httpx.AsyncClient, stop: asyncio.Event, out: list[float]) -> None:
    while not stop.is_set():
        started = time.perf_counter()
        await client.get("/api/health")
        out.append(time.perf_counter() - started)
        await asyncio.sleep(0.1)


async def run(target: str, email: str, password: str, logins: int, rate: float) -> dict:
    limits = httpx.Limits(max_connections=logins + 5)
    async with httpx.AsyncClient(base_url=target, timeout=60, limits=limits) as client:
        # Warm the connection and prove the credentials before measuring.
        _, status = await login(client, email, password)
        if status != 200:
            raise SystemExit(f"login returned {status}; check --email and --password")

        idle: list[float] = []
        stop = asyncio.Event()
        task = asyncio.create_task(probe(client, stop, idle))
        await asyncio.sleep(3)
        stop.set()
        await task

        during: list[float] = []
        stop = asyncio.Event()
        task = asyncio.create_task(probe(client, stop, during))
        pending = []
        for _ in range(logins):
            pending.append(asyncio.create_task(login(client, email, password)))
            await asyncio.sleep(1 / rate)
        results = await asyncio.gather(*pending)
        stop.set()
        await task

    return {
        "target": target,
        "logins": logins,
        "rate_per_s": rate,
        "login": summary([elapsed for elapsed, _ in results]),
        "login_failures": sum(1 for _, code in results if code != 200),
        "probe_idle": summary(idle),
        "probe_during_logins": summary(during),
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--target", default="http://127.0.0.1:3000")
    parser.add_argument("--email", required=True)
    parser.add_argument("--password", required=True)
    parser.add_argument("--logins", type=int, default=50)
    parser.add_argument("--rate", type=float, default=5.0, help="logins started per second")
    args = parser.parse_args()
    check_target(args.target)
    if not 1 <= args.logins <= 200 or not 0 < args.rate <= 20:
        raise SystemExit("--logins 1..200 and --rate up to 20")
    result = asyncio.run(run(args.target, args.email, args.password, args.logins, args.rate))
    json.dump(result, sys.stdout, indent=2)
    print()


if __name__ == "__main__":
    main()
