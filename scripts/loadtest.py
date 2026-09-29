#!/usr/bin/env python3
"""Bounded load test of the APP (not the simulator): N concurrent virtual operators polling the dashboard APIs.
Records concurrency, duration, RPS, latency percentiles, error count -> docs/evidence/loadtest.json"""
from __future__ import annotations

import argparse
import asyncio
import json
import statistics
import time

import httpx

ap = argparse.ArgumentParser()
ap.add_argument("--app", default="http://localhost:8080")
ap.add_argument("--users", type=int, default=20)
ap.add_argument("--seconds", type=int, default=15)
ap.add_argument("--out", default="docs/evidence/loadtest.json")
a = ap.parse_args()
PATHS = ["/api/state", "/api/recommendations", "/api/decisions", "/api/health/services", "/api/forecast/station-mirpur/DIESEL", "/readyz"]


async def user(c, stop, lat, errs):
    i = 0
    while time.time() < stop:
        p = PATHS[i % len(PATHS)]
        i += 1
        t0 = time.perf_counter()
        try:
            r = await c.get(p)
            if r.status_code >= 500:
                errs.append((p, r.status_code))
        except Exception as e:  # noqa: BLE001
            errs.append((p, type(e).__name__))
        lat.append(time.perf_counter() - t0)
        await asyncio.sleep(0.05)          # think time


async def main():
    lat, errs = [], []
    async with httpx.AsyncClient(base_url=a.app, timeout=10, limits=httpx.Limits(max_connections=a.users)) as c:
        t0 = time.time()
        await asyncio.gather(*(user(c, t0 + a.seconds, lat, errs) for _ in range(a.users)))
        dur = time.time() - t0
    lat.sort()
    q = lambda p: round(1000 * lat[min(len(lat) - 1, int(p * len(lat)))], 1)  # noqa: E731
    res = dict(app=a.app, concurrent_users=a.users, duration_s=round(dur, 1), requests=len(lat), rps=round(len(lat) / dur, 1),
               p50_ms=q(.5), p95_ms=q(.95), p99_ms=q(.99), max_ms=round(1000 * lat[-1], 1), mean_ms=round(1000 * statistics.mean(lat), 1),
               errors=len(errs), error_sample=errs[:5], paths=PATHS,
               note="Load is against the app's cached read APIs; simulator traffic is bounded by the app's own refresh loop (~2s).")
    json.dump(res, open(a.out, "w"), indent=1)
    print(json.dumps(res, indent=1))


asyncio.run(main())
