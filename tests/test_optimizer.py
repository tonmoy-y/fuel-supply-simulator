"""LP optimizer + what-if simulation tests. All run against the MOCK-derived World (not the real simulator)."""
from __future__ import annotations

import types

import pytest

import fuelops.optimizer as opt
from fuelops.engine import make_budgets, simulate_allocation, validate_allocation
from fuelops.optimizer import lp_recommend
from tests.test_forecast_engine import run, world_from


def spike_world(sim, mult=2.5):
    run(sim, 30)
    sim.events.append(dict(id=1, type="demand_spike", start_tick=sim.tick + 1, end_tick=sim.tick + 40, status="SCHEDULED",
                           parameters={"multiplier": mult, "region_ids": ["region-dhaka"]}))
    run(sim, 6)
    return world_from(sim)


def req_of(r):
    return dict(source_depot_id=r["source_depot_id"], destination_station_id=r["station_id"], route_id=r["route_id"],
                fuel_type=r["fuel_type"], quantity=r["quantity"])


def test_lp_no_recommendation_when_no_deficit(sim):
    run(sim, 8)
    stats = {}
    assert lp_recommend(world_from(sim), stats=stats) == []
    assert stats["status"] in ("no_deficit", "optimal")


def test_lp_spike_recommendations_are_jointly_valid_and_fast(sim):
    w = spike_world(sim)
    stats = {}
    recs = lp_recommend(w, stats=stats)
    assert recs and stats["status"] == "optimal"
    assert stats.get("rejected_by_validator", 0) == 0     # LP constraints alone must already be feasible
    assert stats["runtime_ms"] < 1000
    b = make_budgets(w)
    for r in recs:
        assert validate_allocation(w, req_of(r), b) == []
        b.depot_inv[(r["source_depot_id"], r["fuel_type"])] -= r["quantity"]
        b.dispatch_left[r["source_depot_id"]] -= r["quantity"]
        assert r["quantity"] % 100 == 0 and r["quantity"] >= 500
        assert r["explanation"]["expected_impact"]["risk_after"] <= r["explanation"]["expected_impact"]["risk_before"]


def test_lp_avoids_closed_route(sim):
    w = spike_world(sim)
    first = lp_recommend(w)[0]
    w.routes[first["route_id"]] = dict(w.routes[first["route_id"]], status="DISRUPTED")
    stats = {}
    assert all(r["route_id"] != first["route_id"] for r in lp_recommend(w, stats=stats))
    assert stats.get("rejected_by_validator", 0) == 0


def test_lp_respects_scarce_depot_inventory_and_reserve(sim):
    w = spike_world(sim)
    for d in w.depots.values():
        d["inventory"] = {f: 3000.0 for f in d["inventory"]}
    recs = lp_recommend(w)
    shipped = {}
    for r in recs:
        k = (r["source_depot_id"], r["fuel_type"])
        shipped[k] = shipped.get(k, 0) + r["quantity"]
    for (dep, f), q in shipped.items():
        assert q <= 3000 - 0.05 * w.depots[dep]["capacity"][f] + 1e-6


def test_lp_respects_dispatch_capacity(sim):
    w = spike_world(sim)
    for d in w.depots.values():
        d["dispatch_capacity_per_tick"] = 1000
    per, stats = {}, {}
    for r in lp_recommend(w, stats=stats):
        per[r["source_depot_id"]] = per.get(r["source_depot_id"], 0) + r["quantity"]
    assert all(q <= 1000 for q in per.values())
    assert stats.get("rejected_by_validator", 0) == 0


def test_lp_solver_exception_returns_empty_and_reports(sim, monkeypatch):
    w = spike_world(sim)

    def boom(*a, **k):
        raise RuntimeError("solver crashed")
    monkeypatch.setattr(opt, "linprog", boom)
    stats = {}
    assert lp_recommend(w, stats=stats) == []
    assert stats["status"].startswith("solver_error")


def test_lp_non_optimal_status_returns_empty(sim, monkeypatch):
    w = spike_world(sim)
    monkeypatch.setattr(opt, "linprog", lambda *a, **k: types.SimpleNamespace(status=2, x=None, fun=None))
    stats = {}
    assert lp_recommend(w, stats=stats) == []
    assert stats["status"] == "solver_status_2"


def test_simulate_valid_candidate_reduces_risk(sim):
    w = spike_world(sim)
    r = next(x for x in lp_recommend(w))
    out = simulate_allocation(w, req_of(r))
    assert out["valid"] and out["after"]["risk"] <= out["before"]["risk"]
    assert len(out["after"]["inventory_path_l"]) == out["horizon_ticks"]


@pytest.mark.parametrize("mut,code", [(dict(quantity=10_000_000.0), "ROUTE_CAPACITY_EXCEEDED"),
                                       (dict(route_id="nope"), "NOT_FOUND")])
def test_simulate_invalid_candidate_reports_violation(sim, mut, code):
    w = spike_world(sim)
    r = lp_recommend(w)[0]
    out = simulate_allocation(w, {**req_of(r), **mut})
    assert not out["valid"] and out["violations"][0]["code"] == code
