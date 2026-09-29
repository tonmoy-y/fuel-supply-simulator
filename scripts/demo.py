#!/usr/bin/env python3
"""Scripted demo following the brief's suggested story (normal -> spike -> detect -> recommend -> act ->
crisis -> fault -> fallback -> recovery). Uses simulator /admin/* for AUTHORISED LOCAL self-test only
(step/events/faults). Never calls /admin/reset. Writes docs/evidence/demo-run.json.

  python scripts/demo.py --sim http://localhost:8000 --app http://localhost:8080
"""
from __future__ import annotations

import argparse
import json
import time

import httpx

ap = argparse.ArgumentParser()
ap.add_argument("--sim", default="http://localhost:8000")
ap.add_argument("--app", default="http://localhost:8080")
ap.add_argument("--out", default="docs/evidence/demo-run.json")
ap.add_argument("--token", default="")
a = ap.parse_args()
S, A = httpx.Client(base_url=a.sim, timeout=10), httpx.Client(base_url=a.app, timeout=15)
H = {"X-Operator-Token": a.token} if a.token else {}
ev: list[dict] = []


def note(step: str, **kw):
    ev.append({"step": step, "t": round(time.time(), 2), **kw})
    print(f"[{len(ev):02d}] {step}: " + json.dumps(kw, default=str)[:300])


def step(n=1):
    for _ in range(n):
        S.post("/admin/step").raise_for_status()
    time.sleep(0.4)
    A.get("/api/state")            # let the app sync via its own loop


def wait_for(fn, timeout=12, every=0.4):
    t0 = time.time()
    while time.time() - t0 < timeout:
        v = fn()
        if v:
            return v
        time.sleep(every)
    return None


def state():
    return A.get("/api/state").json()


def recs():
    return [r for r in A.get("/api/recommendations").json()["items"] if r["route_id"]]


assert A.get("/healthz").json()["status"] == "ok"
note("liveness", app=A.get("/healthz").json(), sim=S.get("/v1/health").json())
inst = S.get("/v1/instance").json()
step(max(0, 30 - inst["tick"]))
st = state()
note("normal_operations", tick=st["instance"]["tick"], mode=st["freshness"]["mode"], engine=st["engine"],
     service_level=st["metrics"]["service_level"], briefing=st["briefing"])

S.post("/admin/events", json={"type": "demand_spike", "start_tick": st["instance"]["tick"] + 1, "duration_ticks": 24,
                              "parameters": {"region_ids": ["region-dhaka"], "multiplier": 2.6}}).raise_for_status()
step(4)
st = state()
note("crisis_detected", briefing=st["briefing"], anomalies=st["anomalies"][:2],
     crisis_incidents=[i["type"] for i in st["incidents"] if i["kind"] == "crisis" and i["status"] != "RECOVERED"])
rs = wait_for(recs)
assert rs, "no recommendation produced"
top = rs[0]
note("recommendation", headline=top["explanation"]["headline"], impact=top["explanation"]["expected_impact"],
     constraints=top["explanation"]["constraints"], review=top["requires_review"])
sim_out = A.post("/api/simulate", json=dict(source_depot_id=top["source_depot_id"], destination_station_id=top["station_id"],
                 route_id=top["route_id"], fuel_type=top["fuel_type"], quantity=top["quantity"])).json()
note("simulate_what_if", valid=sim_out.get("valid"), risk_before=sim_out["before"]["risk"], risk_after=sim_out["after"]["risk"],
     no_simulator_write=True)
res = A.post(f"/api/recommendations/{top['id']}/execute", json={"actor": "demo-operator"}, headers=H).json()
note("allocation_submitted", outcome=res["outcome"], allocation=res.get("allocation"))
replay = A.post(f"/api/recommendations/{top['id']}/execute", json={"actor": "demo-operator"}, headers=H).json()
note("idempotent_replay", outcome=replay["outcome"], detail=replay.get("detail"))
step(1 + 4)
alloc = S.get("/v1/allocations").json()[0]
note("allocation_outcome", status=alloc["status"], expected=alloc["expected_arrival_tick"], actual=alloc["actual_arrival_tick"])

S.post("/admin/events", json={"type": "route_disruption", "start_tick": S.get("/v1/instance").json()["tick"] + 1,
                              "duration_ticks": 8, "parameters": {"route_ids": ["route-gazipur-mirpur"]}}).raise_for_status()
step(2)
note("route_disruption", routes=[r["id"] for r in state()["routes"] if r["status"] == "DISRUPTED"],
     recs=[(r["station_id"], r["fuel_type"], r["route_id"]) for r in recs()][:4])

S.post("/admin/faults", json={"type": "unavailable", "duration_seconds": 6}).raise_for_status()
time.sleep(3.5)
rz = A.get("/readyz")
st = state()
note("api_fault_injected", readyz=rz.status_code, mode=st["freshness"]["mode"],
     service_faults=[i["type"] for i in st["incidents"] if i["kind"] == "service_fault" and i["status"] != "RECOVERED"],
     last_known_state_still_served=bool(st["stations"]))
S.post("/admin/faults/clear")
ok = wait_for(lambda: state()["freshness"]["mode"] == "NORMAL", timeout=25)
note("recovered_from_fault", mode=state()["freshness"]["mode"], recovered=bool(ok))

S.post("/admin/faults", json={"type": "stale_data", "duration_seconds": 8}).raise_for_status()
time.sleep(2.5)
rs = recs()
if rs:
    r2 = A.post(f"/api/recommendations/{rs[0]['id']}/execute", json={"actor": "demo-operator"}, headers=H).json()
    note("stale_data_gate", outcome=r2["outcome"], code=(r2.get("decision") or {}).get("code"))
else:
    note("stale_data_gate", skipped="no recommendation active at this moment")
S.post("/admin/faults/clear")
wait_for(lambda: not state()["freshness"]["stale"], timeout=15)

A.post("/api/policy", json={"mode": "fallback"}, headers=H)
note("model_unavailable_fallback", engine=state()["engine"])
A.post("/api/policy", json={"mode": "auto"}, headers=H)
step(30)
for _ in range(40):
    for r in recs():
        if not r["requires_review"]:
            A.post(f"/api/recommendations/{r['id']}/execute", json={"actor": "demo-autopilot"}, headers=H)
    step(1)
    if all(i["status"] == "RECOVERED" for i in state()["incidents"] if i["kind"] == "crisis"):
        break
st = state()
m = S.get("/v1/metrics").json()
note("recovery", crisis=[(i["type"], i["status"]) for i in st["incidents"] if i["kind"] == "crisis"], metrics=m,
     decisions=len(A.get("/api/decisions").json()))
json.dump(ev, open(a.out, "w"), indent=1, default=str)
print(f"\nEvidence written to {a.out}")
