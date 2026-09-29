from __future__ import annotations

import pytest

from fuelops.engine import World, assess_all, fallback_policy, make_budgets, recommend, validate_allocation
from fuelops.forecast import FUELS, detect_anomalies, fit_model, prior_level
from mocksim.server import Sim


def world_from(sim: Sim, models=None, history=None) -> World:
    st = {k: dict(v, inventory=dict(v["inventory"]), capacity=dict(v["capacity"])) for k, v in sim.stations.items()}
    dp = {k: dict(v, inventory=dict(v["inventory"]), capacity=dict(v["capacity"])) for k, v in sim.depots.items()}
    ms = {}
    for s in st.values():
        for f in FUELS:
            r = [x for x in sim.demand if x["station_id"] == s["id"] and x["fuel_type"] == f][-240:]
            ms[(s["id"], f)] = fit_model(s, f, r, sim.tick_minutes)
    return World(tick=sim.tick, sim_time=sim.sim_time(), tick_minutes=sim.tick_minutes, depots=dp, stations=st,
                 routes={k: dict(v) for k, v in sim.routes.items()}, allocations=[dict(a) for a in sim.allocs],
                 arrivals=[], events=[], models=models or ms)


def run(sim, n):
    for _ in range(n):
        sim.tick_once()


# ---------------------------------------------------------------- forecast
def test_static_prior_when_no_history(sim):
    s = sim.stations["station-mirpur"]
    m = fit_model(s, "DIESEL", [], 15)
    assert m.source == "static-prior" and m.confidence == "low"
    assert m.level == pytest.approx(prior_level("urban_high", "DIESEL", "region-dhaka", 15))


def test_ewma_tracks_actual_demand_and_is_confident(sim):
    run(sim, 60)
    s = sim.stations["station-mirpur"]
    rows = [r for r in sim.demand if r["station_id"] == s["id"]]
    m = fit_model(s, "DIESEL", rows, 15)
    assert m.source == "ewma" and m.confidence == "high" and m.n_obs >= 24
    assert m.level == pytest.approx(prior_level("urban_high", "DIESEL", "region-dhaka", 15), rel=0.15)


def test_forecast_error_reasonable_on_baseline(sim):
    run(sim, 60)
    s = sim.stations["station-tongi"]
    rows = [r for r in sim.demand if r["station_id"] == s["id"]]
    m = fit_model(s, "DIESEL", rows, 15)
    pred = m.per_tick(sim.sim_time(), 1)
    sim.tick_once()
    actual = [r for r in sim.demand if r["station_id"] == s["id"] and r["fuel_type"] == "DIESEL"][-1]["demand_liters"]
    assert abs(pred - actual) / actual < 0.25


def test_spike_detected_from_history_and_anomaly_flagged(sim):
    run(sim, 40)
    sim.events.append(dict(id=1, type="demand_spike", start_tick=sim.tick + 1, end_tick=sim.tick + 30, status="SCHEDULED",
                           parameters={"multiplier": 2.5, "region_ids": ["region-dhaka"]}))
    run(sim, 4)
    s = sim.stations["station-mirpur"]
    rows = [r for r in sim.demand if r["station_id"] == s["id"]]
    base_s = dict(s, demand_multiplier=1.0)   # ignore the live multiplier: prove history alone detects it
    m = fit_model(base_s, "DIESEL", rows, 15)
    assert m.spike
    assert detect_anomalies(base_s, "DIESEL", rows, m)


# ---------------------------------------------------------------- engine
def test_baseline_start_has_no_recommendations(sim):
    run(sim, 8)
    assert recommend(world_from(sim)) == []


def test_demand_spike_produces_valid_explainable_recommendations(sim):
    run(sim, 30)
    sim.events.append(dict(id=1, type="demand_spike", start_tick=sim.tick + 1, end_tick=sim.tick + 40, status="SCHEDULED",
                           parameters={"multiplier": 2.5, "region_ids": ["region-dhaka"]}))
    run(sim, 6)
    w = world_from(sim)
    recs = [r for r in recommend(w) if r["route_id"]]
    assert recs, "spike must create at least one recommendation"
    b = make_budgets(w)
    for r in recs:
        req = dict(source_depot_id=r["source_depot_id"], destination_station_id=r["station_id"], route_id=r["route_id"],
                   fuel_type=r["fuel_type"], quantity=r["quantity"])
        ex = r["explanation"]
        assert ex["headline"] and ex["constraints"] and ex["expected_impact"]["risk_after"] < ex["expected_impact"]["risk_before"]
        assert r["idempotency_key"].startswith("fuelops-") and len(r["idempotency_key"]) < 150
        assert validate_allocation(w, req, b) == []          # jointly valid: budgets are shared
        b.depot_inv[(req["source_depot_id"], req["fuel_type"])] -= req["quantity"]
        b.dispatch_left[req["source_depot_id"]] -= req["quantity"]
    by_depot = {}
    for r in recs:
        by_depot[r["source_depot_id"]] = by_depot.get(r["source_depot_id"], 0) + r["quantity"]
    for d, q in by_depot.items():
        assert q <= w.depots[d]["dispatch_capacity_per_tick"]


def test_route_disruption_reroutes_to_alternative(sim):
    run(sim, 30)
    sim.stations["station-mirpur"]["inventory"]["DIESEL"] = 700.0
    sim.routes["route-gazipur-mirpur"]["status"] = "DISRUPTED"
    recs = [r for r in recommend(world_from(sim)) if r["station_id"] == "station-mirpur" and r["fuel_type"] == "DIESEL"]
    assert recs and recs[0]["route_id"] == "route-patiya-mirpur"
    assert any("DISRUPTED" in a.get("reason", "") for a in recs[0]["explanation"]["alternatives"])


def test_station_outage_is_blocked_not_recommended(sim):
    run(sim, 10)
    sim.stations["station-tongi"]["status"] = "OUTAGE"
    a = [x for x in assess_all(world_from(sim)) if x.station_id == "station-tongi"]
    assert all(x.severity == "BLOCKED" for x in a)
    assert not [r for r in recommend(world_from(sim)) if r["station_id"] == "station-tongi"]


def test_no_feasible_route_yields_review_item(sim):
    run(sim, 30)
    sim.stations["station-mirpur"]["inventory"]["DIESEL"] = 300.0
    for r in ("route-gazipur-mirpur", "route-patiya-mirpur"):
        sim.routes[r]["status"] = "DISRUPTED"
    recs = [r for r in recommend(world_from(sim)) if r["station_id"] == "station-mirpur" and r["fuel_type"] == "DIESEL"]
    assert recs and recs[0]["route_id"] is None and recs[0]["requires_review"]


def test_low_confidence_requires_human_review(sim):
    w = world_from(sim, models={})   # placeholder, replaced below
    ms = {(s, f): fit_model(w.stations[s], f, [], 15) for s in w.stations for f in FUELS}   # static prior => low
    w.models = ms
    w.stations["station-tongi"]["inventory"]["DIESEL"] = 500.0
    recs = [r for r in recommend(w) if r["route_id"]]
    assert recs and all(r["requires_review"] for r in recs)


@pytest.mark.parametrize("mut,code", [
    (lambda w, q: q.update(route_id="route-gazipur-tongi"), "ROUTE_MISMATCH"),
    (lambda w, q: w.routes["route-gazipur-mirpur"].update(status="DISRUPTED"), "ROUTE_DISRUPTED"),
    (lambda w, q: w.stations["station-mirpur"].update(status="OUTAGE"), "STATION_CLOSED"),
    (lambda w, q: w.depots["depot-gazipur"].update(status="OUTAGE"), "DEPOT_CLOSED"),
    (lambda w, q: q.update(quantity=7001), "ROUTE_CAPACITY_EXCEEDED"),
    (lambda w, q: w.depots["depot-gazipur"]["inventory"].update(DIESEL=100), "INSUFFICIENT_INVENTORY"),
    (lambda w, q: w.stations["station-mirpur"]["inventory"].update(DIESEL=14000), "DESTINATION_CAPACITY_EXCEEDED"),
    (lambda w, q: q.update(source_depot_id="nope"), "NOT_FOUND"),
    (lambda w, q: q.update(quantity=-5), "VALIDATION_ERROR"),
])
def test_validation_layer_mirrors_simulator_codes(sim, mut, code):
    w = world_from(sim)
    req = dict(source_depot_id="depot-gazipur", destination_station_id="station-mirpur", route_id="route-gazipur-mirpur",
               fuel_type="DIESEL", quantity=3000.0)
    mut(w, req)
    assert validate_allocation(w, req)[0]["code"] == code


def test_dispatch_budget_enforced(sim):
    w = world_from(sim)
    b = make_budgets(w)
    b.dispatch_left["depot-gazipur"] = 1000
    req = dict(source_depot_id="depot-gazipur", destination_station_id="station-mirpur", route_id="route-gazipur-mirpur",
               fuel_type="DIESEL", quantity=3000.0)
    assert validate_allocation(w, req, b)[0]["code"] == "DISPATCH_CAPACITY_EXCEEDED"


def test_pending_allocations_count_as_commitments_no_duplicate_shipments(sim):
    run(sim, 30)
    sim.stations["station-tongi"]["inventory"]["DIESEL"] = 1500.0
    first = [r for r in recommend(world_from(sim)) if r["route_id"] and r["station_id"] == "station-tongi" and r["fuel_type"] == "DIESEL"]
    assert first
    sim.allocs.append(dict(id=1, idempotency_key="x", source_depot_id=first[0]["source_depot_id"],
                           destination_station_id="station-tongi", route_id=first[0]["route_id"], fuel_type="DIESEL",
                           quantity=first[0]["quantity"], created_tick=sim.tick, departure_tick=None,
                           expected_arrival_tick=None, actual_arrival_tick=None, status="PENDING", failure_reason=None))
    again = [r for r in recommend(world_from(sim)) if r["route_id"] and r["station_id"] == "station-tongi" and r["fuel_type"] == "DIESEL"]
    assert sum(r["quantity"] for r in again) < first[0]["quantity"]


def test_fallback_policy_produces_valid_allocations(sim):
    run(sim, 10)
    sim.stations["station-karnaphuli"]["inventory"]["PETROL"] = 1000.0
    w = world_from(sim)
    recs = fallback_policy(w)
    assert recs and recs[0]["explanation"]["uncertainty"]["model"] == "fallback-rule"
    r = recs[0]
    assert validate_allocation(w, dict(source_depot_id=r["source_depot_id"], destination_station_id=r["station_id"],
                                       route_id=r["route_id"], fuel_type=r["fuel_type"], quantity=r["quantity"])) == []


def test_scheduled_route_disruption_is_avoided_before_it_starts(sim):
    """Regression: benchmark showed an allocation created the tick before a scheduled disruption FAILED at dispatch."""
    run(sim, 30)
    sim.stations["station-tongi"]["inventory"]["DIESEL"] = 900.0
    w = world_from(sim)
    w.events = [dict(id=7, type="route_disruption", start_tick=w.tick + 1, end_tick=w.tick + 10, status="SCHEDULED",
                     parameters={"route_ids": ["route-gazipur-tongi"]})]
    recs = [r for r in recommend(w) if r["station_id"] == "station-tongi" and r["fuel_type"] == "DIESEL"]
    assert recs and all(r["route_id"] != "route-gazipur-tongi" for r in recs)
    alts = [a for r in recs for a in r["explanation"]["alternatives"] if a.get("route_id") == "route-gazipur-tongi"]
    assert alts and "would FAIL at dispatch" in alts[0]["reason"]
    w.events[0]["start_tick"] = w.tick + 5          # starts after departure -> still usable
    assert any(r["route_id"] == "route-gazipur-tongi" for r in recommend(w) if r["station_id"] == "station-tongi")
