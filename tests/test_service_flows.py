"""End-to-end flows: FuelOps service <-> MOCK simulator (in-process). Labelled mock, not real-sim evidence."""
from __future__ import annotations

import asyncio

from tests.conftest import step


async def event(admin, type_, start, dur, **params):
    r = await admin.post("/admin/events", json={"type": type_, "start_tick": start, "duration_ticks": dur, "parameters": params})
    assert r.status_code == 201


async def fault(admin, type_, secs=30, **p):
    assert (await admin.post("/admin/faults", json={"type": type_, "duration_seconds": secs, "parameters": p})).status_code == 201


def executable(ops):
    return [r for r in ops.analysis["recommendations"] if r["route_id"]]


async def test_happy_path_recommend_execute_arrive(ops, admin, sim):
    await step(admin, 40)
    await ops.refresh()
    assert ops.mode == "NORMAL" and ops.analysis["engine"] == "heuristic+forecast"
    await sim_low_stock(sim, ops)
    recs = executable(ops)
    assert recs
    r = recs[0]
    res = await ops.execute(r["id"], actor="test")
    assert res["outcome"] == "ACCEPTED" and res["allocation"]["status"] == "PENDING"
    again = await ops.execute(r["id"], actor="test")                  # app-level + sim-level idempotency
    assert again["outcome"] == "ACCEPTED" and len(sim.allocs) == 1
    await step(admin, 1 + 4)
    await ops.refresh()
    a = ops.data["allocations"][0]
    assert a["status"] == "ARRIVED" and a["actual_arrival_tick"] == a["expected_arrival_tick"]
    assert ops.decisions[-1]["outcome"] == "ACCEPTED" and ops.decisions[-1]["allocation_id"] == a["id"]


async def sim_low_stock(sim, ops):
    sim.stations["station-tongi"]["inventory"]["DIESEL"] = 900.0
    await ops.refresh()


async def test_execute_rejected_locally_when_state_changed(ops, admin, sim):
    await step(admin, 30)
    await sim_low_stock(sim, ops)
    r = executable(ops)[0]
    sim.routes[r["route_id"]]["status"] = "DISRUPTED"                 # world changes before operator approves
    res = await ops.execute(r["id"], actor="test")
    assert res["outcome"] == "REJECTED_LOCAL" and res["decision"]["code"] == "ROUTE_DISRUPTED"
    assert not sim.allocs                                              # nothing sent to simulator


async def test_simulator_409_is_recorded_and_not_retried(ops, admin, sim, transport):
    await step(admin, 30)
    await sim_low_stock(sim, ops)
    r = executable(ops)[0]
    ops.rec_cache[r["id"]] = dict(r, quantity=r["quantity"])
    orig = ops.world
    def bad_world():                   # local view says fine, simulator disagrees (e.g. race)
        w = orig()
        w.depots[r["source_depot_id"]]["inventory"][r["fuel_type"]] = 10**9
        return w
    ops.world = bad_world              # type: ignore
    sim.depots[r["source_depot_id"]]["inventory"][r["fuel_type"]] = 10.0
    res = await ops.execute(r["id"], actor="test")
    assert res["outcome"] == "REJECTED_SIM" and res["decision"]["code"] == "INSUFFICIENT_INVENTORY"
    assert sum(1 for c in transport.calls if c == ("POST", "/v1/allocations")) == 1


async def test_demand_spike_detect_respond_recover(ops, admin, sim):
    await step(admin, 30)
    await ops.refresh()
    await event(admin, "demand_spike", 32, 20, multiplier=2.6, region_ids=["region-dhaka"])
    await step(admin, 4)
    await ops.refresh()
    assert any(i["kind"] == "crisis" and i["type"] == "demand_spike" and i["status"] == "OPEN" for i in ops.incidents.values())
    recs = executable(ops)
    assert recs, "spike must raise recommendations"
    served = 0
    for r in recs[:3]:
        if (await ops.execute(r["id"], actor="test"))["outcome"] == "ACCEPTED":
            served += 1
    assert served
    for _ in range(60):
        await step(admin, 1)
        await ops.refresh()
        for r in executable(ops):
            if not r["requires_review"]:
                await ops.execute(r["id"], actor="test")
        if all(i["status"] == "RECOVERED" for i in ops.incidents.values() if i["kind"] == "crisis"):
            break
    crisis = [i for i in ops.incidents.values() if i["kind"] == "crisis"]
    assert crisis and all(i["status"] == "RECOVERED" for i in crisis)
    assert (await ops.c.metrics())[0]["service_level"] > 0.93


async def test_route_disruption_reroute_and_pending_failure_accounted(ops, admin, sim):
    await step(admin, 30)
    await event(admin, "route_disruption", 31, 10, route_ids=["route-gazipur-mirpur"])
    await step(admin, 2)
    sim.stations["station-mirpur"]["inventory"]["DIESEL"] = 800.0
    await ops.refresh()
    recs = [r for r in executable(ops) if r["station_id"] == "station-mirpur" and r["fuel_type"] == "DIESEL"]
    assert recs and recs[0]["route_id"] == "route-patiya-mirpur"
    assert (await ops.execute(recs[0]["id"]))["outcome"] == "ACCEPTED"


async def test_station_outage_and_depot_constraint_and_shipment_events(ops, admin, sim):
    await step(admin, 12)
    await event(admin, "station_outage", 13, 6, station_ids=["station-tongi"])
    await event(admin, "depot_constraint", 13, 6, depot_ids=["depot-patiya"])
    await event(admin, "shipment_delay", 13, 1, delay_ticks=3)
    await event(admin, "supply_shortfall", 13, 1, factor=0.5)
    await step(admin, 2)
    await ops.refresh()
    st = {s["id"]: s for s in ops.data["stations"]}
    assert st["station-tongi"]["status"] == "OUTAGE"
    assert {d["id"]: d["status"] for d in ops.data["depots"]}["depot-patiya"] == "CONSTRAINED"
    assert any(a["status"] == "DELAYED" for a in ops.data["arrivals"])
    assert all(a["severity"] == "BLOCKED" for a in ops.analysis["assessments"] if a["station_id"] == "station-tongi")
    types = {i["type"] for i in ops.incidents.values() if i["kind"] == "crisis"}
    assert {"station_outage", "depot_constraint"} <= types


async def test_combined_crisis_engine_still_valid_and_respects_shared_budgets(ops, admin, sim):
    await step(admin, 30)
    await event(admin, "demand_spike", 31, 30, multiplier=3.0)
    await event(admin, "route_disruption", 31, 30, route_ids=["route-gazipur-mirpur", "route-patiya-karnaphuli"])
    await step(admin, 5)
    await ops.refresh()
    recs = executable(ops)
    per_depot = {}
    for r in recs:
        per_depot[r["source_depot_id"]] = per_depot.get(r["source_depot_id"], 0) + r["quantity"]
    for d, q in per_depot.items():
        assert q <= ops.world().depots[d]["dispatch_capacity_per_tick"]
    assert all(ops.world().routes[r["route_id"]]["status"] == "AVAILABLE" for r in recs)


async def test_api_unavailable_degrades_gates_and_recovers(ops, admin, sim):
    await step(admin, 30)
    await ops.refresh()
    assert ops.mode == "NORMAL"
    good_state = ops.data["stations"]
    await fault(admin, "unavailable")
    await ops.refresh()
    assert ops.mode in ("DEGRADED", "OFFLINE") and ops.data["stations"] == good_state     # last-known-good kept
    inc = ops.incidents["svc-api_unavailable"]              # all endpoints failing => unavailable (not partial errors)
    assert inc["kind"] == "service_fault" and inc["status"] == "OPEN"
    await admin.post("/admin/faults/clear")
    await ops.refresh()
    assert ops.c.breaker_is_open and ops.mode == "DEGRADED"         # breaker fails fast until cooldown ends
    await asyncio.sleep(ops.s.breaker_cooldown_s + 0.05)
    for _ in range(4):
        await ops.refresh()
    assert ops.mode == "NORMAL"
    open_faults = [i for i in ops.incidents.values() if i["kind"] == "service_fault" and i["status"] != "RECOVERED"]
    assert not open_faults, open_faults


async def test_stale_data_blocks_execution_unless_forced(ops, admin, sim):
    await step(admin, 30)
    await sim_low_stock(sim, ops)
    r = executable(ops)[0]
    await fault(admin, "stale_data")
    res = await ops.execute(r["id"])
    assert res["outcome"] == "REJECTED_LOCAL" and res["decision"]["code"] == "STALE_DATA" and not sim.allocs
    assert ops.incidents["svc-stale_data"]["kind"] == "service_fault"
    assert all(x["requires_review"] for x in executable(ops))            # stale => human review flag
    await admin.post("/admin/faults/clear")
    for _ in range(3):
        await ops.refresh()
    assert ops.stale is False and ops.incidents["svc-stale_data"]["status"] == "RECOVERED"


async def test_transient_failure_defers_then_retries_with_same_key(ops, admin, sim):
    await step(admin, 30)
    await sim_low_stock(sim, ops)
    r = executable(ops)[0]
    orig = ops.c.create_allocation
    calls = {"n": 0}
    from fuelops.simclient import SimTransient
    async def flaky(body):
        calls["n"] += 1
        if calls["n"] == 1:
            raise SimTransient("boom")
        return await orig(body)
    ops.c.create_allocation = flaky     # type: ignore
    res = await ops.execute(r["id"])
    assert res["outcome"] == "DEFERRED" and ops.deferred
    await ops._retry_deferred()
    assert not ops.deferred and len(sim.allocs) == 1
    assert sim.allocs[0]["idempotency_key"] == r["idempotency_key"]


async def test_model_unavailable_falls_back_to_rule_policy(ops, admin, sim):
    await step(admin, 20)
    sim.stations["station-tongi"]["inventory"]["DIESEL"] = 1500.0
    ops.policy_mode = "fallback"
    await ops.refresh()
    assert ops.analysis["engine"] == "fallback-rule"
    assert ops.incidents["svc-fallback_active"]["kind"] == "service_fault"
    recs = executable(ops)
    assert recs and all(r["requires_review"] for r in recs)
    ops.policy_mode = "auto"
    for _ in range(3):
        await ops.refresh()
    assert ops.analysis["engine"] == "heuristic+forecast" and ops.incidents["svc-fallback_active"]["status"] == "RECOVERED"


async def test_history_unavailable_uses_static_prior_and_low_confidence(ops, admin, sim, monkeypatch):
    await step(admin, 30)
    async def boom(*a, **k):
        from fuelops.simclient import SimTransient
        raise SimTransient("history down")
    monkeypatch.setattr(ops.c, "demand_history", boom)
    await ops.refresh()
    assert ops.analysis["engine"] == "heuristic+static-prior"
    assert {a["confidence"] for a in ops.analysis["assessments"]} == {"low"}


async def test_sse_events_trigger_rest_refresh_and_reconnect_resyncs(settings, transport, admin, sim):
    from fuelops.config import Settings
    from fuelops.service import FuelOps
    from fuelops.simclient import SimClient, SimTransient
    from fuelops.telemetry import Telemetry
    t = Telemetry()
    c = SimClient(Settings(**{**settings.__dict__, "enable_sse": True}), t, transport=transport)
    o = FuelOps(Settings(**{**settings.__dict__, "enable_sse": True}), c, t)
    seq = [[("__connected__", {}), ("simulation.tick", {"tick": 1})], SimTransient("drop"), [("__connected__", {})]]
    async def fake_stream():
        item = seq.pop(0) if seq else None
        if item is None:
            await asyncio.sleep(3600)
        if isinstance(item, Exception):
            raise item
        for x in item:
            yield x
    c.stream = fake_stream               # type: ignore
    await step(admin, 3)
    o._running = True
    task = asyncio.create_task(o._sse_loop())
    for _ in range(80):
        await asyncio.sleep(0.05)
        if not seq and o.sse_connected and o.data["instance"]:
            break
    task.cancel()
    assert o.data["instance"]["tick"] == 3                        # state came from REST after (re)connect
    assert t.sse_reconnects._value.get() >= 1
