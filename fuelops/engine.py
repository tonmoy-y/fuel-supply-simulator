"""Decision engine: shortage-risk projection, constrained allocation heuristic, safety validation.

Pipeline per (station, fuel):
  1. project inventory path over the horizon: inv - forecast demand + committed inbound
     (PENDING / IN_TRANSIT allocations), with a growing uncertainty band
  2. risk = max_t P(inventory_t <= 0)  (normal approximation)
  3. if risk >= trigger: for every feasible route pick quantity by bisection so the
     *extended-horizon* risk falls to target, subject to ALL simulator constraints
  4. choose route by lowest residual risk, then shortest transit, then healthiest depot
  5. depots' inventory / dispatch budgets are shared across recommendations, most-urgent first
Everything is deterministic and explainable; nothing here calls the simulator.
"""
from __future__ import annotations

import hashlib
import math
from dataclasses import dataclass
from datetime import datetime, timedelta

from .forecast import FUELS, Model, norm_cdf

SEVERITY_ORDER = {"CRITICAL": 0, "HIGH": 1, "WATCH": 2, "OK": 3, "BLOCKED": 4}


@dataclass
class World:
    tick: int
    sim_time: datetime
    tick_minutes: int
    depots: dict[str, dict]
    stations: dict[str, dict]
    routes: dict[str, dict]
    allocations: list[dict]
    arrivals: list[dict]
    events: list[dict]
    models: dict[tuple[str, str], Model]
    stale: bool = False


@dataclass
class Budgets:
    """Mutable per-cycle budgets so several recommendations never overdraw a depot."""
    depot_inv: dict[tuple[str, str], float]
    dispatch_left: dict[str, float]
    station_inbound: dict[tuple[str, str], list[tuple[int, float]]]


# ------------------------------------------------------------------ projection
def inbound_commitments(w: World) -> dict[tuple[str, str], list[tuple[int, float]]]:
    out: dict[tuple[str, str], list[tuple[int, float]]] = {}
    for a in w.allocations:
        if a["status"] == "PENDING":
            r = w.routes.get(a["route_id"])
            off = 1 + (r["transit_ticks"] if r else 2)
        elif a["status"] == "IN_TRANSIT":
            off = max(1, int(a["expected_arrival_tick"]) - w.tick)
        else:
            continue
        out.setdefault((a["destination_station_id"], a["fuel_type"]), []).append((off, float(a["quantity"])))
    return out


def project(w: World, st: dict, fuel: str, inbound: list[tuple[int, float]], horizon: int):
    """-> (means[1..H], stds[1..H], per_tick_forecast[1..H])"""
    m = w.models[(st["id"], fuel)]
    inv = float(st["inventory"][fuel])
    means, stds, fc = [], [], []
    cum, var = 0.0, 0.0
    model_unc = 0.10 if m.confidence == "low" else 0.05
    for k in range(1, horizon + 1):
        d = m.per_tick(w.sim_time, k)
        fc.append(d)
        cum += d
        var += (m.sigma_rel * d) ** 2
        arrived = sum(q for off, q in inbound if off <= k)
        means.append(inv + arrived - cum)     # station capacity is enforced at allocation time
        stds.append(math.sqrt(var) + model_unc * cum + 1e-6)
    return means, stds, fc


def risk_of(means: list[float], stds: list[float]) -> float:
    return max(norm_cdf(-mu / sd) for mu, sd in zip(means, stds))


def hours_to_stockout(means: list[float], inv: float, tick_minutes: int) -> float | None:
    prev = inv
    for i, mu in enumerate(means):
        if mu <= 0:
            frac = prev / (prev - mu) if prev - mu > 0 else 0
            return round((i + frac) * tick_minutes / 60, 2)
        prev = mu
    return None


def severity_for(risk: float) -> str:
    return "CRITICAL" if risk >= 0.5 else "HIGH" if risk >= 0.2 else "WATCH" if risk >= 0.05 else "OK"


def route_disrupted_at(w: World, route_id: str, tick: int) -> dict | None:
    """Known (ACTIVE or SCHEDULED) route_disruption event covering `route_id` at `tick` (the departure tick)."""
    for e in w.events:
        if e.get("type") != "route_disruption" or e.get("status") not in ("ACTIVE", "SCHEDULED"):
            continue
        ids = (e.get("parameters") or {}).get("route_ids") or []
        if (not ids or route_id in ids) and e["start_tick"] <= tick < e["end_tick"]:
            return e
    return None


# ------------------------------------------------------------------ validation
def make_budgets(w: World) -> Budgets:
    disp = {}
    for d in w.depots.values():
        used = sum(a["quantity"] for a in w.allocations if a["source_depot_id"] == d["id"] and
                   (a["status"] == "PENDING" or (a["status"] == "IN_TRANSIT" and a.get("departure_tick") == w.tick)))
        disp[d["id"]] = float(d["dispatch_capacity_per_tick"]) - used
    return Budgets({(d["id"], f): float(d["inventory"][f]) for d in w.depots.values() for f in FUELS},
                   disp, inbound_commitments(w))


def validate_allocation(w: World, req: dict, b: Budgets | None = None) -> list[dict]:
    """Mirror of the simulator's documented validation order (guide 5.2). Returns violations."""
    b = b or make_budgets(w)
    d, s, r = (w.depots.get(req["source_depot_id"]), w.stations.get(req["destination_station_id"]),
               w.routes.get(req["route_id"]))
    f, q = req["fuel_type"], float(req["quantity"])
    v = lambda code, msg: [{"code": code, "message": msg}]  # noqa: E731
    if not (d and s and r):
        return v("NOT_FOUND", "unknown depot/station/route id")
    if f not in FUELS or q <= 0:
        return v("VALIDATION_ERROR", "fuel_type must be DIESEL|PETROL|OCTANE and quantity > 0")
    if r["source_depot_id"] != d["id"] or r["destination_station_id"] != s["id"]:
        return v("ROUTE_MISMATCH", "route endpoints differ from request")
    if d["status"] not in ("OPEN", "CONSTRAINED"):
        return v("DEPOT_CLOSED", f"depot status {d['status']}")
    if s["status"] != "OPEN":
        return v("STATION_CLOSED", f"station status {s['status']}")
    if r["status"] != "AVAILABLE":
        return v("ROUTE_DISRUPTED", f"route status {r['status']}")
    if q > r["max_shipment"]:
        return v("ROUTE_CAPACITY_EXCEEDED", f"{q:.0f} > route max {r['max_shipment']}")
    if b.depot_inv[(d["id"], f)] < q:
        return v("INSUFFICIENT_INVENTORY", f"depot has {b.depot_inv[(d['id'], f)]:.0f} L {f}")
    if q > b.dispatch_left[d["id"]]:
        return v("DISPATCH_CAPACITY_EXCEEDED", f"dispatch budget left {b.dispatch_left[d['id']]:.0f} L this tick")
    inbound_now = sum(x for _, x in b.station_inbound.get((s["id"], f), []))
    if s["inventory"][f] + q > s["capacity"][f]:
        return v("DESTINATION_CAPACITY_EXCEEDED", f"station {s['inventory'][f]:.0f}+{q:.0f} > cap {s['capacity'][f]}")
    if s["inventory"][f] + inbound_now + q > s["capacity"][f]:
        return v("DESTINATION_OVERCOMMIT", f"with {inbound_now:.0f} L already inbound would exceed capacity")
    return []


# ------------------------------------------------------------------ recommendation
@dataclass
class Assessment:
    station_id: str
    fuel: str
    inventory: float
    capacity: float
    fill_pct: float
    forecast_next_6h: float
    inbound_l: float
    risk: float
    severity: str
    hours_to_stockout: float | None
    confidence: str
    model_source: str
    spike: bool
    blocked_reason: str | None = None


def assess_all(w: World, b: Budgets | None = None) -> list[Assessment]:
    b = b or make_budgets(w)
    H = 24
    out = []
    for st in w.stations.values():
        for f in FUELS:
            m = w.models[(st["id"], f)]
            inbound = b.station_inbound.get((st["id"], f), [])
            means, stds, fc = project(w, st, f, inbound, H)
            risk = risk_of(means, stds)
            sev = severity_for(risk)
            blocked = None
            if st["status"] != "OPEN":
                blocked, sev = f"station {st['status']}", "BLOCKED"
            out.append(Assessment(st["id"], f, float(st["inventory"][f]), float(st["capacity"][f]),
                                  round(100 * st["inventory"][f] / st["capacity"][f], 1), round(sum(fc[:24]), 1),
                                  round(sum(q for _, q in inbound), 1), round(risk, 4), sev,
                                  hours_to_stockout(means, st["inventory"][f], w.tick_minutes),
                                  m.confidence, m.source, m.spike, blocked))
    return sorted(out, key=lambda a: (SEVERITY_ORDER[a.severity], -a.risk))


def _rid(tick: int, route: str, fuel: str, qty: float) -> str:
    return "rec-" + hashlib.sha1(f"{tick}|{route}|{fuel}|{qty:.0f}".encode()).hexdigest()[:10]


def recommend(w: World, trigger: float = 0.10, target: float = 0.05, min_ship: float = 500.0,
              horizon: int = 24) -> list[dict]:
    """Return explainable recommendations (dicts), most urgent first."""
    b = make_budgets(w)
    assessments = assess_all(w, b)
    ext = min(48, horizon * 2)
    recs: list[dict] = []
    for a in assessments:
        if a.severity in ("OK", "BLOCKED") or a.risk < trigger:
            continue
        st = w.stations[a.station_id]
        f = a.fuel
        m = w.models[(st["id"], f)]
        for _round in range(2):   # allow a 2nd shipment via another route if one truck-load is not enough
            inbound = b.station_inbound.get((st["id"], f), [])
            means, stds, _ = project(w, st, f, inbound, horizon)
            risk_now = risk_of(means, stds)
            if risk_now < (trigger if _round == 0 else target * 2):
                break
            hts_before = hours_to_stockout(means, st["inventory"][f], w.tick_minutes)
            headroom = st["capacity"][f] - st["inventory"][f] - sum(q for _, q in inbound)
            cands, rejected = [], []
            for r in w.routes.values():
                if r["destination_station_id"] != st["id"]:
                    continue
                d = w.depots[r["source_depot_id"]]
                why = None
                if r["status"] != "AVAILABLE":
                    why = f"route {r['status']}"
                elif d["status"] not in ("OPEN", "CONSTRAINED"):
                    why = f"depot {d['status']}"
                elif (ev := route_disrupted_at(w, r["id"], w.tick + 1)):
                    why = f"route disruption event #{ev.get('id')} covers departure tick {w.tick + 1} (would FAIL at dispatch)"
                reserve = d["capacity"][f] * (0.02 if a.severity == "CRITICAL" else 0.10)
                avail = b.depot_inv[(d["id"], f)] - reserve
                qmax = min(r["max_shipment"], avail, b.dispatch_left[d["id"]], headroom)
                if not why and qmax < min_ship:
                    why = (f"only {max(qmax, 0):.0f} L shippable (route max {r['max_shipment']}, "
                           f"depot spare {max(avail, 0):.0f}, dispatch left {b.dispatch_left[d['id']]:.0f}, "
                           f"station headroom {max(headroom, 0):.0f})")
                if why:
                    rejected.append({"route_id": r["id"], "depot_id": d["id"], "reason": why})
                    continue
                arr_off = 1 + r["transit_ticks"]
                lo, hi = 0.0, qmax
                for _ in range(24):   # bisection: smallest Q reaching target risk on extended horizon
                    mid = (lo + hi) / 2
                    mm, ss, _ = project(w, st, f, inbound + [(arr_off, mid)], ext)
                    lo, hi = (lo, mid) if risk_of(mm, ss) <= target else (mid, hi)
                q = min(qmax, math.ceil(max(hi, min_ship) / 100) * 100)
                q = min(q, math.floor(qmax / 100) * 100) if qmax >= 100 else q
                if q < min_ship:
                    rejected.append({"route_id": r["id"], "depot_id": d["id"], "reason": "quantity below minimum"})
                    continue
                mm, ss, _ = project(w, st, f, inbound + [(arr_off, q)], horizon)
                mm2, ss2, _ = project(w, st, f, inbound + [(arr_off, q)], ext)
                cands.append(dict(route=r, depot=d, qty=float(q), risk_after=risk_of(mm, ss),
                                  risk_after_ext=risk_of(mm2, ss2), arr_off=arr_off,
                                  hts_after=hours_to_stockout(mm, st["inventory"][f], w.tick_minutes),
                                  cover=b.depot_inv[(d["id"], f)] / d["capacity"][f]))
            if not cands:
                if not recs or recs[-1]["station_id"] != st["id"] or recs[-1]["fuel_type"] != f:
                    recs.append(_blocked_rec(w, a, m, rejected))
                break
            cands.sort(key=lambda c: (round(c["risk_after"], 3), c["route"]["transit_ticks"], -c["cover"]))
            best = cands[0]
            r, d, q = best["route"], best["depot"], best["qty"]
            req = dict(source_depot_id=d["id"], destination_station_id=st["id"], route_id=r["id"],
                       fuel_type=f, quantity=q)
            viol = validate_allocation(w, req, b)
            if viol:   # safety layer refuses to even propose it
                rejected.append({"route_id": r["id"], "depot_id": d["id"], "reason": viol[0]["message"]})
                break
            exp_tick = w.tick + best["arr_off"]
            depot_after = b.depot_inv[(d["id"], f)] - q
            review_reasons = []
            if m.confidence == "low":
                review_reasons.append("low forecast confidence")
            if w.stale:
                review_reasons.append("simulator data flagged stale")
            if depot_after < 0.10 * d["capacity"][f]:
                review_reasons.append("would drop depot below 10% reserve")
            recs.append(dict(
                id=_rid(w.tick, r["id"], f, q), created_tick=w.tick, severity=a.severity if _round == 0 else "HIGH",
                station_id=st["id"], fuel_type=f, source_depot_id=d["id"], route_id=r["id"], quantity=q,
                expected_arrival_tick=exp_tick,
                expected_arrival_time=(w.sim_time + timedelta(minutes=w.tick_minutes * best["arr_off"])).isoformat(),
                idempotency_key=f"fuelops-t{w.tick}-{r['id']}-{f}-{int(q)}",
                requires_review=bool(review_reasons), review_reasons=review_reasons,
                explanation=dict(
                    headline=(f"{st['id']} {f}: stock-out risk {risk_now:.0%}"
                              + (f", empty in ~{hts_before:.1f} h" if hts_before is not None else "")
                              + f" -> send {q:.0f} L from {d['id']}"),
                    why_at_risk=dict(inventory_l=round(st["inventory"][f]), capacity_l=st["capacity"][f],
                                     inbound_committed_l=round(sum(x for _, x in inbound)),
                                     forecast_demand_6h_l=round(a.forecast_next_6h),
                                     demand_regime="SPIKE" if m.spike else "normal",
                                     station_demand_multiplier=st.get("demand_multiplier", 1.0),
                                     active_events=[e["type"] for e in w.events if e["status"] == "ACTIVE"]),
                    signals=[f"model={m.source} n_obs={m.n_obs} level={m.level:.1f} L/tick",
                             f"uncertainty sigma_rel={m.sigma_rel:.2f}",
                             f"hours_to_stockout_before={hts_before}"],
                    constraints=[
                        f"route max shipment {r['max_shipment']} L", f"depot inventory {b.depot_inv[(d['id'], f)]:.0f} L "
                        f"(after: {depot_after:.0f})", f"dispatch budget {b.dispatch_left[d['id']]:.0f} L this tick",
                        f"station headroom {headroom:.0f} L", f"transit {r['transit_ticks']} ticks (+1 tick to depart)"],
                    expected_impact=dict(risk_before=round(risk_now, 3), risk_after=round(best["risk_after"], 3),
                                         risk_after_extended=round(best["risk_after_ext"], 3),
                                         hours_to_stockout_before=hts_before, hours_to_stockout_after=best["hts_after"]),
                    uncertainty=dict(confidence=m.confidence, model=m.source, data_stale=w.stale),
                    alternatives=[dict(route_id=c["route"]["id"], quantity=c["qty"],
                                       risk_after=round(c["risk_after"], 3), transit_ticks=c["route"]["transit_ticks"])
                                  for c in cands[1:]] + rejected,
                )))
            # commit to budgets so later recs cannot overdraw
            b.depot_inv[(d["id"], f)] -= q
            b.dispatch_left[d["id"]] -= q
            b.station_inbound.setdefault((st["id"], f), []).append((best["arr_off"], q))
    return recs


def _blocked_rec(w: World, a: Assessment, m: Model, rejected: list[dict]) -> dict:
    return dict(id=_rid(w.tick, "none", a.fuel, 0) + a.station_id[-3:], created_tick=w.tick, severity=a.severity,
                station_id=a.station_id, fuel_type=a.fuel, source_depot_id=None, route_id=None, quantity=0.0,
                expected_arrival_tick=None, expected_arrival_time=None, idempotency_key=None,
                requires_review=True, review_reasons=["no feasible allocation - operator action needed"],
                explanation=dict(headline=f"{a.station_id} {a.fuel}: risk {a.risk:.0%} but NO feasible allocation",
                                 why_at_risk=dict(inventory_l=round(a.inventory), hours_to_stockout=a.hours_to_stockout),
                                 signals=[], constraints=[], expected_impact={}, alternatives=rejected,
                                 uncertainty=dict(confidence=m.confidence, model=m.source)))


# ------------------------------------------------------------------ fallback policy
def fallback_policy(w: World, threshold: float = 0.30, min_ship: float = 500.0) -> list[dict]:
    """Model-free rule used when the forecaster/engine is unavailable:
    refill any station/fuel below 30% of capacity to ~60% from the depot with most stock."""
    b = make_budgets(w)
    recs = []
    for st in w.stations.values():
        if st["status"] != "OPEN":
            continue
        for f in FUELS:
            inv, cap = st["inventory"][f], st["capacity"][f]
            if inv / cap >= threshold:
                continue
            best = None
            for r in w.routes.values():
                if r["destination_station_id"] != st["id"] or r["status"] != "AVAILABLE":
                    continue
                d = w.depots[r["source_depot_id"]]
                if d["status"] not in ("OPEN", "CONSTRAINED"):
                    continue
                q = min(0.6 * cap - inv, r["max_shipment"], b.depot_inv[(d["id"], f)] * 0.5, b.dispatch_left[d["id"]])
                q = math.floor(q / 100) * 100
                if q >= min_ship and (best is None or b.depot_inv[(d["id"], f)] > best[0]):
                    best = (b.depot_inv[(d["id"], f)], r, d, q)
            if best:
                _, r, d, q = best
                req = dict(source_depot_id=d["id"], destination_station_id=st["id"], route_id=r["id"],
                           fuel_type=f, quantity=float(q))
                if validate_allocation(w, req, b):
                    continue
                b.depot_inv[(d["id"], f)] -= q
                b.dispatch_left[d["id"]] -= q
                b.station_inbound.setdefault((st["id"], f), []).append((1 + r["transit_ticks"], q))
                recs.append(dict(
                    id=_rid(w.tick, r["id"], f, q), created_tick=w.tick, severity="HIGH", station_id=st["id"],
                    fuel_type=f, source_depot_id=d["id"], route_id=r["id"], quantity=float(q),
                    expected_arrival_tick=w.tick + 1 + r["transit_ticks"], expected_arrival_time=None,
                    idempotency_key=f"fuelops-fb-t{w.tick}-{r['id']}-{f}-{int(q)}",
                    requires_review=False, review_reasons=["fallback policy active (no forecast)"],
                    explanation=dict(headline=f"FALLBACK: {st['id']} {f} at {100 * inv / cap:.0f}% of capacity -> "
                                              f"send {q:.0f} L from {d['id']}",
                                     why_at_risk=dict(inventory_l=round(inv), capacity_l=cap),
                                     signals=["threshold rule (<30% capacity)"], constraints=[], expected_impact={},
                                     alternatives=[], uncertainty=dict(confidence="low", model="fallback-rule"))))
    return recs


def simulate_allocation(w: World, req: dict, horizon: int = 24) -> dict:
    """What-if ("Simulate" step of the brief's engineering loop): validate a candidate allocation and project
    station inventory with and without it. Read-only - never touches the simulator."""
    b = make_budgets(w)
    viol = validate_allocation(w, req, b)
    st, r = w.stations.get(req.get("destination_station_id")), w.routes.get(req.get("route_id"))
    f = req.get("fuel_type")
    out: dict = dict(valid=not viol, violations=viol, horizon_ticks=horizon)
    if not st or not r or f not in FUELS or (st["id"], f) not in w.models:
        return out
    inbound = b.station_inbound.get((st["id"], f), [])
    arr = 1 + r["transit_ticks"]
    q = float(req.get("quantity") or 0)

    def side(inb):
        means, stds, _ = project(w, st, f, inb, horizon)
        return dict(risk=round(risk_of(means, stds), 3), hours_to_stockout=hours_to_stockout(means, st["inventory"][f], w.tick_minutes),
                    inventory_path_l=[round(m) for m in means])
    out.update(before=side(inbound), after=side(inbound + [(arr, q)]), arrives_tick=w.tick + arr,
               depot_inventory_after_l=round(b.depot_inv[(req["source_depot_id"], f)] - q) if req.get("source_depot_id") in w.depots else None,
               disclaimer="Simulated projection from forecast means; no simulator state was changed.")
    return out
