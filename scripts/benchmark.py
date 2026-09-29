#!/usr/bin/env python3
"""Deterministic policy benchmark on the MOCK world (same seed, same injected events for every policy).
Policies: none (no allocations) | rule (model-free threshold fallback) | engine (forecast + risk + constrained heuristic) | lp (linear program, fuelops/optimizer.py).
Measures service level / unmet liters / allocation failures over 192 ticks (48 sim-hours).
NOTE: numbers come from the mock (approximated demand noise & supply), NOT the real simulator."""
from __future__ import annotations

import asyncio
import json
import sys

import httpx

sys.path.insert(0, ".")
import time  # noqa: E402

from fuelops.engine import World, fallback_policy, recommend  # noqa: E402
from fuelops.forecast import FUELS, fit_model  # noqa: E402
from fuelops.optimizer import lp_recommend  # noqa: E402
from mocksim.server import Sim, build_app  # noqa: E402

TICKS = 192
SCENARIOS = {
    "baseline": [],
    "demand_spike_dhaka_2.6x": [("demand_spike", 40, 24, {"region_ids": ["region-dhaka"], "multiplier": 2.6})],
    "spike+route_disruption": [("demand_spike", 40, 24, {"multiplier": 2.2}),
                               ("route_disruption", 44, 16, {"route_ids": ["route-gazipur-mirpur", "route-patiya-karnaphuli"]})],
    "combined_crisis": [("demand_spike", 40, 24, {"multiplier": 2.4}), ("route_disruption", 44, 16, {"route_ids": ["route-gazipur-tongi"]}),
                        ("depot_constraint", 40, 30, {"depot_ids": ["depot-patiya"]}), ("shipment_delay", 42, 1, {"delay_ticks": 6}),
                        ("station_outage", 60, 6, {"station_ids": ["station-coxsbazar"]})],
}


def world(sim: Sim) -> World:
    st = {k: v for k, v in sim.stations.items()}
    ms = {}
    for s in st.values():
        rows = [r for r in sim.demand[-4000:] if r["station_id"] == s["id"]][-240:]
        for f in FUELS:
            ms[(s["id"], f)] = fit_model(s, f, rows, sim.tick_minutes)
    return World(tick=sim.tick, sim_time=sim.sim_time(), tick_minutes=sim.tick_minutes, depots=sim.depots, stations=st,
                 routes=sim.routes, allocations=sim.allocs, arrivals=sim.arrivals, events=sim.events, models=ms)


async def run(policy: str, events) -> dict:
    sim = Sim(seed=12345)
    app = build_app(sim)
    decide_s, solves = 0.0, 0
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://m") as c:
        for t, s, d, p in events:
            await c.post("/admin/events", json={"type": t, "start_tick": s, "duration_ticks": d, "parameters": p})
        for _ in range(TICKS):
            if policy != "none":
                w = world(sim)
                t0 = time.perf_counter()
                recs = {"engine": recommend, "lp": lp_recommend}.get(policy, fallback_policy)(w)
                decide_s += time.perf_counter() - t0
                solves += 1
                for r in recs:
                    if r["route_id"]:
                        await c.post("/v1/allocations", json=dict(idempotency_key=r["idempotency_key"], source_depot_id=r["source_depot_id"],
                                     destination_station_id=r["station_id"], route_id=r["route_id"], fuel_type=r["fuel_type"], quantity=r["quantity"]))
            await c.post("/admin/step")
        m = (await c.get("/v1/metrics")).json()
    return dict(service_level=round(m["service_level"], 4), unmet_l=round(m["unmet_demand_liters"]), served_l=round(m["served_demand_liters"]),
                shipped_l=round(m["allocation_liters"]), failures=m["allocation_failures"],
                mean_decision_ms=round(1000 * decide_s / solves, 2) if solves else 0.0)


async def main():
    out = {}
    for name, ev in SCENARIOS.items():
        out[name] = {p: await run(p, ev) for p in ("none", "rule", "engine", "lp")}
        print(f"\n{name}")
        for p, r in out[name].items():
            print(f"  {p:7s} service_level={r['service_level']:.4f} unmet={r['unmet_l']:>7} L shipped={r['shipped_l']:>7} L failures={r['failures']} decide={r['mean_decision_ms']}ms")
    json.dump(dict(ticks=TICKS, seed=12345, world="MOCK", results=out), open("docs/evidence/benchmark.json", "w"), indent=1)


asyncio.run(main())
