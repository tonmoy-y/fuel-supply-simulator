"""Linear-programming allocator (alternative to the greedy engine in engine.py).

Formulation (one solve per decision cycle, deterministic inputs = forecast means + safety margin):

  sets      R  = feasible (route r, fuel f) pairs  (route AVAILABLE, depot OPEN/CONSTRAINED, station OPEN,
                 no known disruption covering the departure tick)
            S  = (station s, fuel f) pairs, k = 1..H ticks ahead
  variables x[r,f] >= 0        litres shipped on route r this tick            (<= route max_shipment)
            u[s,f,k] >= 0      projected shortfall (litres) at station s, fuel f, tick k
  minimise  sum_k W_k * u[s,f,k]  +  eps * sum x
  s.t.      base[s,f,k] + sum_{r->s, off_r<=k} x[r,f] + u[s,f,k] >= z * sd[s,f,k]      (coverage, every k)
            sum_{r from d} x[r,f] <= depot_inv[d,f] - reserve[d,f]                    (depot inventory)
            sum_{r from d, all f} x <= dispatch_left[d]                                (depot dispatch capacity)
            sum_{r->s} x[r,f] <= station_capacity - inventory - inbound               (destination headroom)
where base[s,f,k] = inventory + committed inbound - cumulative forecast demand (from engine.project),
sd = forecast std, z = 1.645 (~5% stock-out risk target), off_r = 1 + transit_ticks.

LP (not MILP) is sufficient: quantities are continuous litres and every constraint is linear. The only
discrete rules (100 L granularity, 500 L minimum shipment) are applied afterwards by rounding and every
result is re-checked by engine.validate_allocation, so an invalid allocation is never proposed even if
the solver returns one.  Nothing here calls the simulator.
"""
from __future__ import annotations

import math
import time
from datetime import timedelta

import numpy as np
from scipy.optimize import linprog
from scipy.sparse import lil_matrix

from .engine import (
    World,
    _rid,
    hours_to_stockout,
    inbound_commitments,
    make_budgets,
    project,
    risk_of,
    route_disrupted_at,
    validate_allocation,
)
from .forecast import FUELS

Z = 1.645
EPS_SHIP = 1e-3


def lp_recommend(w: World, horizon: int = 24, min_ship: float = 500.0, time_limit_s: float = 2.0,
                 stats: dict | None = None) -> list[dict]:
    """Solve the allocation LP and return recommendations in the same shape as engine.recommend().
    On solver failure/infeasibility returns [] and records the reason in `stats` (caller falls back)."""
    t0 = time.perf_counter()
    stats = stats if stats is not None else {}
    stats.update(status="no_deficit", n_vars=0, n_cons=0, runtime_ms=0.0)
    b = make_budgets(w)
    inbound = inbound_commitments(w)

    # ---- feasible shipping variables
    xs = []   # (route, depot, fuel)
    for r in w.routes.values():
        d, st = w.depots.get(r["source_depot_id"]), w.stations.get(r["destination_station_id"])
        if not d or not st or r["status"] != "AVAILABLE" or d["status"] not in ("OPEN", "CONSTRAINED") \
                or st["status"] != "OPEN" or route_disrupted_at(w, r["id"], w.tick + 1):
            continue
        for f in FUELS:
            xs.append((r, d, f))
    pairs = [(s, f) for s in w.stations.values() if s["status"] == "OPEN" for f in FUELS]

    # ---- coverage rows
    base: dict[tuple[str, str], tuple[list[float], list[float]]] = {}
    for s, f in pairs:
        means, stds, _ = project(w, s, f, inbound.get((s["id"], f), []), horizon)
        base[(s["id"], f)] = (means, stds)
    # only optimise pairs that could run short (skips solving pure no-op problems)
    at_risk = [(s, f) for s, f in pairs if min(m - Z * sd for m, sd in zip(*base[(s["id"], f)])) < 0]
    if not at_risk or not xs:
        stats["runtime_ms"] = round(1000 * (time.perf_counter() - t0), 2)
        return []
    uidx = {(s["id"], f, k): len(xs) + i for i, (s, f, k) in
            enumerate((s, f, k) for s, f in at_risk for k in range(1, horizon + 1))}
    n = len(xs) + len(uidx)
    c = np.zeros(n)
    c[: len(xs)] = EPS_SHIP
    for (_, _, k), j in uidx.items():
        c[j] = 1.0 + 0.5 * (horizon - k) / horizon      # earlier shortfalls hurt slightly more

    rhs: list[float] = []
    A_ub = lil_matrix((len(uidx) + len(w.depots) * 4 + len(w.stations) * 3 + len(w.depots), n))
    ri = 0
    for (sid, f, k), j in uidx.items():        # -(sum x) - u <= base - z*sd
        means, stds = base[(sid, f)]
        for xi, (r, _, xf) in enumerate(xs):
            if xf == f and r["destination_station_id"] == sid and 1 + r["transit_ticks"] <= k:
                A_ub[ri, xi] = -1.0
        A_ub[ri, j] = -1.0
        rhs.append(means[k - 1] - Z * stds[k - 1])
        ri += 1
    for d in w.depots.values():
        for f in FUELS:
            idx = [i for i, (_, dd, xf) in enumerate(xs) if dd["id"] == d["id"] and xf == f]
            if idx:
                reserve = 0.05 * d["capacity"][f]
                for i in idx:
                    A_ub[ri, i] = 1.0
                rhs.append(max(0.0, b.depot_inv[(d["id"], f)] - reserve))
                ri += 1
        idx = [i for i, (_, dd, _) in enumerate(xs) if dd["id"] == d["id"]]
        if idx:
            for i in idx:
                A_ub[ri, i] = 1.0
            rhs.append(max(0.0, b.dispatch_left[d["id"]]))
            ri += 1
    for s in w.stations.values():
        for f in FUELS:
            idx = [i for i, (r, _, xf) in enumerate(xs) if xf == f and r["destination_station_id"] == s["id"]]
            if idx:
                inb = sum(q for _, q in inbound.get((s["id"], f), []))
                for i in idx:
                    A_ub[ri, i] = 1.0
                rhs.append(max(0.0, s["capacity"][f] - s["inventory"][f] - inb))
                ri += 1
    A_ub = A_ub[:ri].tocsr()
    bounds = [(0.0, float(r["max_shipment"])) for r, _, _ in xs] + [(0.0, None)] * len(uidx)
    stats.update(n_vars=n, n_cons=ri)
    try:
        res = linprog(c, A_ub=A_ub, b_ub=np.array(rhs), bounds=bounds, method="highs",
                      options={"time_limit": time_limit_s})
    except Exception as e:  # noqa: BLE001
        stats.update(status=f"solver_error:{type(e).__name__}", runtime_ms=round(1000 * (time.perf_counter() - t0), 2))
        return []
    stats["runtime_ms"] = round(1000 * (time.perf_counter() - t0), 2)
    if res.status != 0 or res.x is None:
        stats["status"] = f"solver_status_{res.status}"
        return []
    stats["status"] = "optimal"
    stats["objective"] = round(float(res.fun), 3)

    # ---- round, filter, independently validate, commit budgets (most-constrained first = largest qty)
    plan = sorted(((float(res.x[i]), i) for i in range(len(xs))), reverse=True)
    recs: list[dict] = []
    for raw, i in plan:
        r, d, f = xs[i]
        q = math.floor(raw / 100) * 100
        if q < min_ship:
            continue
        st = w.stations[r["destination_station_id"]]
        req = dict(source_depot_id=d["id"], destination_station_id=st["id"], route_id=r["id"],
                   fuel_type=f, quantity=float(q))
        if validate_allocation(w, req, b):
            stats["rejected_by_validator"] = stats.get("rejected_by_validator", 0) + 1
            continue
        arr_off = 1 + r["transit_ticks"]
        inb = b.station_inbound.get((st["id"], f), [])
        m_before, s_before, _ = project(w, st, f, inb, horizon)
        m_after, s_after, _ = project(w, st, f, inb + [(arr_off, float(q))], horizon)
        mdl = w.models[(st["id"], f)]
        reasons = (["low forecast confidence"] if mdl.confidence == "low" else []) + \
                  (["simulator data flagged stale"] if w.stale else [])
        recs.append(dict(
            id=_rid(w.tick, r["id"], f, q), created_tick=w.tick, severity="HIGH", station_id=st["id"], fuel_type=f,
            source_depot_id=d["id"], route_id=r["id"], quantity=float(q), expected_arrival_tick=w.tick + arr_off,
            expected_arrival_time=(w.sim_time + timedelta(minutes=w.tick_minutes * arr_off)).isoformat(),
            idempotency_key=f"fuelops-lp-t{w.tick}-{r['id']}-{f}-{int(q)}",
            requires_review=bool(reasons), review_reasons=reasons,
            explanation=dict(
                headline=f"LP: {st['id']} {f} -> send {q:.0f} L from {d['id']} (risk {risk_of(m_before, s_before):.0%} -> "
                         f"{risk_of(m_after, s_after):.0%})",
                why_at_risk=dict(inventory_l=round(st["inventory"][f]), capacity_l=st["capacity"][f]),
                signals=[f"LP objective {stats['objective']}, {stats['n_vars']} vars, {stats['n_cons']} rows, "
                         f"{stats['runtime_ms']} ms"],
                constraints=["route max shipment", "depot inventory less 5% reserve", "depot dispatch capacity",
                             "station headroom", "route availability incl. scheduled disruptions"],
                expected_impact=dict(risk_before=round(risk_of(m_before, s_before), 3),
                                     risk_after=round(risk_of(m_after, s_after), 3),
                                     hours_to_stockout_before=hours_to_stockout(m_before, st["inventory"][f], w.tick_minutes)),
                alternatives=[], uncertainty=dict(confidence=mdl.confidence, model=mdl.source, data_stale=w.stale))))
        b.depot_inv[(d["id"], f)] -= q
        b.dispatch_left[d["id"]] -= q
        b.station_inbound.setdefault((st["id"], f), []).append((arr_off, float(q)))
    return recs
