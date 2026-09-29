"""MOCK BUP Fuel Supply Simulator.

Built ONLY from the written Integration Guide so the platform can be tested when the
real Docker image (asifmahmoud414/bup-fuel-supply-simulator) is unavailable.
Numbers such as demand noise are approximations; results from this mock are labelled
"mock" everywhere and are NOT evidence of behaviour against the real simulator.
"""
from __future__ import annotations

import asyncio
import json
import random
import threading
import time
from datetime import datetime, timedelta, timezone
from typing import Any

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse, StreamingResponse
from pydantic import BaseModel, Field

FUELS = ["DIESEL", "PETROL", "OCTANE"]
REGIONS = [
    {"id": "region-dhaka", "name": "Dhaka Division", "demand_factor": 1.00},
    {"id": "region-chattogram", "name": "Chattogram Division", "demand_factor": 1.08},
]
DEPOTS = [
    ("depot-gazipur", "Gazipur Depot", "region-dhaka", 12000,
     (90000, 70000, 45000), (60000, 45000, 26000)),
    ("depot-patiya", "Patiya Depot", "region-chattogram", 11000,
     (85000, 65000, 40000), (55000, 42000, 24000)),
]
STATIONS = [
    ("station-mirpur", "Mirpur Fuel Station", "region-dhaka", "urban_high",
     (15000, 14000, 9000), (9000, 9000, 5000)),
    ("station-tongi", "Tongi Fuel Station", "region-dhaka", "industrial",
     (18000, 9000, 6000), (11000, 6000, 3500)),
    ("station-karnaphuli", "Karnaphuli Fuel Station", "region-chattogram", "highway",
     (14000, 15000, 9000), (8500, 9500, 5200)),
    ("station-coxsbazar", "Cox's Bazar Fuel Station", "region-chattogram", "regional",
     (12000, 12000, 7000), (7500, 7500, 4200)),
]
ROUTES = [
    ("route-gazipur-mirpur", "depot-gazipur", "station-mirpur", 2, 7000),
    ("route-gazipur-tongi", "depot-gazipur", "station-tongi", 2, 6500),
    ("route-patiya-karnaphuli", "depot-patiya", "station-karnaphuli", 2, 7000),
    ("route-patiya-coxsbazar", "depot-patiya", "station-coxsbazar", 3, 6000),
    ("route-gazipur-karnaphuli", "depot-gazipur", "station-karnaphuli", 4, 5000),
    ("route-patiya-mirpur", "depot-patiya", "station-mirpur", 4, 5000),
]
DAILY = {  # liters/day: DIESEL, PETROL, OCTANE, noise
    "urban_high": (8500, 10500, 5600, 0.10),
    "industrial": (14000, 4500, 2200, 0.08),
    "highway": (10500, 11000, 6200, 0.12),
    "regional": (7200, 7600, 3600, 0.10),
}


def hour_factor(profile: str, hour: int) -> float:
    if profile == "industrial":
        return 1.55 if 6 <= hour <= 17 else 0.45
    if profile == "highway":
        return 1.35 if (6 <= hour <= 9 or 16 <= hour <= 20) else 0.75
    if profile == "urban_high":
        return 1.45 if (7 <= hour <= 9 or 16 <= hour <= 20) else 0.70
    return 1.25 if 7 <= hour <= 20 else 0.65


class AllocIn(BaseModel):
    idempotency_key: str = Field(min_length=1, max_length=150)
    source_depot_id: str
    destination_station_id: str
    route_id: str
    fuel_type: str
    quantity: float = Field(gt=0)


class EventIn(BaseModel):
    type: str
    start_tick: int = Field(ge=0)
    duration_ticks: int = Field(gt=0)
    parameters: dict[str, Any] = {}


class FaultIn(BaseModel):
    type: str
    duration_seconds: int = Field(gt=0, le=3600)
    parameters: dict[str, Any] = {}


EVENT_TYPES = {"demand_spike", "route_disruption", "station_outage", "depot_constraint",
               "shipment_delay", "supply_shortfall"}
FAULT_TYPES = {"latency", "unavailable", "error_rate", "stale_data", "stream_disconnect"}


class Sim:
    def __init__(self, seed: int = 12345, tick_minutes: int = 15, start_running: bool = False):
        self.lock = threading.RLock()
        self.seed, self.tick_minutes = seed, tick_minutes
        self.start_running = start_running
        self.subscribers: list[asyncio.Queue] = []
        self.loop: asyncio.AbstractEventLoop | None = None
        self.reset(publish=False)

    # ---- world -------------------------------------------------------------
    def reset(self, publish: bool = True) -> None:
        with self.lock:
            self.tick = 0
            self.t0 = datetime(2026, 1, 1, tzinfo=timezone.utc)
            self.status = "RUNNING" if self.start_running else "PAUSED"
            self.depots = {d[0]: dict(id=d[0], name=d[1], region_id=d[2], status="OPEN",
                                      dispatch_capacity_per_tick=d[3],
                                      capacity=dict(zip(FUELS, d[4])),
                                      inventory={k: float(v) for k, v in zip(FUELS, d[5])})
                           for d in DEPOTS}
            self.stations = {s[0]: dict(id=s[0], name=s[1], region_id=s[2], status="OPEN",
                                        demand_profile=s[3], demand_multiplier=1.0,
                                        capacity=dict(zip(FUELS, s[4])),
                                        inventory={k: float(v) for k, v in zip(FUELS, s[5])})
                             for s in STATIONS}
            self.routes = {r[0]: dict(id=r[0], source_depot_id=r[1], destination_station_id=r[2],
                                      transit_ticks=r[3], max_shipment=r[4], status="AVAILABLE")
                           for r in ROUTES}
            self.arrivals = self._schedule()
            self.events: list[dict] = []
            self.faults: list[dict] = []
            self.allocs: list[dict] = []
            self.demand: list[dict] = []
            self.audit: list[dict] = []
            self.metrics = dict(served=0.0, unmet=0.0)
        if publish:
            self.publish("simulator.notice", {"message": "Simulation reset"})

    def _schedule(self) -> list[dict]:
        reg_daily = {}
        for s in STATIONS:
            for i, f in enumerate(FUELS):
                reg_daily[(s[2], f)] = reg_daily.get((s[2], f), 0) + DAILY[s[3]][i]
        out, n = [], 0
        pairs = [(d[0], d[2], f) for d in DEPOTS for f in FUELS]
        for k, tick in enumerate([12, 14, 16, 20]):
            dep, reg, f = pairs[k % len(pairs)]
            n += 1
            out.append(self._arr(n, dep, f, reg_daily[(reg, f)] * 0.5, tick))
        for rnd in range(1, 4):                       # 3 rounds x 6 (depot, fuel) pairs = 18 arrivals
            for dep, reg, f in pairs:
                n += 1
                out.append(self._arr(n, dep, f, reg_daily[(reg, f)], 64 * rnd))
        return sorted(out, key=lambda a: a["planned_tick"])

    @staticmethod
    def _arr(n, dep, fuel, qty, tick):
        return dict(id=f"supply-{n:03d}", depot_id=dep, fuel_type=fuel, quantity=float(qty),
                    planned_tick=tick, actual_tick=None, status="SCHEDULED")

    def sim_time(self) -> datetime:
        return self.t0 + timedelta(minutes=self.tick * self.tick_minutes)

    def log(self, action, etype=None, eid=None, result="OK", meta=None):
        self.audit.append(dict(id=len(self.audit) + 1,
                               wall_time=datetime.now(timezone.utc).isoformat(),
                               sim_time=self.sim_time().isoformat(), tick=self.tick,
                               action=action, entity_type=etype, entity_id=str(eid) if eid else None,
                               result=result, metadata_json=meta or {}))

    # ---- pub/sub -----------------------------------------------------------
    def publish(self, name: str, payload: dict) -> None:
        for q in list(self.subscribers):
            try:
                if self.loop:
                    self.loop.call_soon_threadsafe(self._put, q, (name, payload))
            except Exception:
                pass

    @staticmethod
    def _put(q, item):
        try:
            q.put_nowait(item)
        except asyncio.QueueFull:
            pass

    # ---- tick --------------------------------------------------------------
    def tick_once(self) -> dict:
        with self.lock:
            self.tick += 1
            self._events()
            self._supply()
            self._allocations()
            self._demand()
            self.log("simulation.tick")
            snap = {"tick": self.tick, "sim_time": self.sim_time().isoformat()}
        self.publish("simulation.tick", snap)
        return snap

    def _events(self):
        for e in self.events:
            if e["status"] == "SCHEDULED" and e["start_tick"] <= self.tick < e["end_tick"]:
                e["status"] = "ACTIVE"
                self._apply(e, True)
                self.log("event.started", "event", e["id"])
            if e["status"] in ("ACTIVE", "SCHEDULED") and self.tick >= e["end_tick"]:
                if e["status"] == "ACTIVE":
                    self._apply(e, False)
                e["status"] = "RESOLVED"
                self.log("event.resolved", "event", e["id"])

    def _apply(self, e, on: bool):
        p, t = e["parameters"], e["type"]
        def pick(coll, key):
            ids = p.get(key) or []
            return [x for x in coll.values() if not ids or x["id"] in ids]
        if t == "demand_spike":
            m = float(p.get("multiplier", 1.5))
            rids = p.get("region_ids") or []
            for s in pick(self.stations, "station_ids"):
                if rids and s["region_id"] not in rids:
                    continue
                s["demand_multiplier"] = s["demand_multiplier"] * m if on else max(s["demand_multiplier"] / m, 0.01)
        elif t == "route_disruption":
            for r in pick(self.routes, "route_ids"):
                r["status"] = "DISRUPTED" if on else "AVAILABLE"
        elif t == "station_outage":
            for s in pick(self.stations, "station_ids"):
                s["status"] = "OUTAGE" if on else "OPEN"
        elif t == "depot_constraint":
            for d in pick(self.depots, "depot_ids"):
                d["status"] = "CONSTRAINED" if on else "OPEN"
        elif t == "shipment_delay" and on:
            for a in self.arrivals:
                if a["status"] != "ARRIVED" and (not p.get("depot_ids") or a["depot_id"] in p["depot_ids"]) \
                        and (not p.get("fuel_types") or a["fuel_type"] in p["fuel_types"]):
                    a["planned_tick"] += int(p.get("delay_ticks", 2))
                    a["status"] = "DELAYED"
            self.arrivals.sort(key=lambda a: a["planned_tick"])
        elif t == "supply_shortfall" and on:
            for a in self.arrivals:
                if a["status"] != "ARRIVED" and (not p.get("depot_ids") or a["depot_id"] in p["depot_ids"]) \
                        and (not p.get("fuel_types") or a["fuel_type"] in p["fuel_types"]):
                    a["quantity"] *= float(p.get("factor", 0.5))

    def _supply(self):
        for a in self.arrivals:
            if a["status"] != "ARRIVED" and a["planned_tick"] <= self.tick:
                d = self.depots[a["depot_id"]]
                d["inventory"][a["fuel_type"]] = min(d["capacity"][a["fuel_type"]],
                                                     d["inventory"][a["fuel_type"]] + a["quantity"])
                a["status"], a["actual_tick"] = "ARRIVED", self.tick
                self.log("supply.arrived", "supply", a["id"])
                self.publish("inventory.updated", dict(entity_type="depot", entity_id=d["id"],
                                                       inventory=d["inventory"]))

    def _allocations(self):
        for a in self.allocs:
            r = self.routes[a["route_id"]]
            if a["status"] == "PENDING":
                if r["status"] != "AVAILABLE":
                    a["status"], a["failure_reason"] = "FAILED", "ROUTE_DISRUPTED"
                    self.depots[a["source_depot_id"]]["inventory"][a["fuel_type"]] += a["quantity"]
                else:
                    a["status"], a["departure_tick"] = "IN_TRANSIT", self.tick
                    a["expected_arrival_tick"] = self.tick + r["transit_ticks"]
                self.log("allocation.departed" if a["status"] == "IN_TRANSIT" else "allocation.failed",
                         "allocation", a["id"])
                self.publish("allocation.status_changed", dict(a))
            elif a["status"] == "IN_TRANSIT" and a["expected_arrival_tick"] <= self.tick:
                s = self.stations[a["destination_station_id"]]
                s["inventory"][a["fuel_type"]] = min(s["capacity"][a["fuel_type"]],
                                                     s["inventory"][a["fuel_type"]] + a["quantity"])
                a["status"], a["actual_arrival_tick"] = "ARRIVED", self.tick
                self.log("allocation.arrived", "allocation", a["id"])
                self.publish("allocation.status_changed", dict(a))

    def _demand(self):
        rng = random.Random(self.seed * 1_000_003 + self.tick)
        hour = self.sim_time().hour
        for s in self.stations.values():
            daily = DAILY[s["demand_profile"]]
            rf = next(r["demand_factor"] for r in REGIONS if r["id"] == s["region_id"])
            for i, f in enumerate(FUELS):
                base = daily[i] / 24 * (self.tick_minutes / 60)
                d = base * hour_factor(s["demand_profile"], hour) * rf * s["demand_multiplier"]
                d *= 1 + rng.uniform(-daily[3], daily[3])
                served = 0.0 if s["status"] != "OPEN" else min(d, s["inventory"][f])
                s["inventory"][f] -= served
                self.metrics["served"] += served
                self.metrics["unmet"] += d - served
                self.demand.append(dict(id=len(self.demand) + 1, station_id=s["id"], fuel_type=f,
                                        tick=self.tick, sim_time=self.sim_time().isoformat(),
                                        demand_liters=round(d, 3), served_liters=round(served, 3),
                                        unmet_liters=round(d - served, 3)))


def build_app(sim: Sim | None = None) -> FastAPI:
    sim = sim or Sim()
    app = FastAPI(title="MOCK BUP Fuel Supply Simulator")
    app.state.sim = sim

    @app.on_event("startup")
    async def _start():
        sim.loop = asyncio.get_running_loop()
        async def runner():
            while True:
                await asyncio.sleep(0.25)
                if sim.status == "RUNNING":
                    sim.tick_once()
        asyncio.create_task(runner())

    def active_faults():
        now = time.time()
        return [f for f in sim.faults if f["active"] and f["end"] > now]

    @app.middleware("http")
    async def faults(request: Request, call_next):
        p = request.url.path
        if not p.startswith("/v1/") or p == "/v1/health":
            return await call_next(request)
        af = {f["type"]: f for f in active_faults()}
        if "latency" in af:
            await asyncio.sleep(af["latency"]["parameters"].get("delay_ms", 500) / 1000)
        if "unavailable" in af:
            return JSONResponse({"error": {"code": "FAULT_INJECTED",
                                           "message": "Simulator API temporarily unavailable."}}, 503)
        if "error_rate" in af and random.random() < af["error_rate"]["parameters"].get("rate", 0.25):
            return JSONResponse({"error": {"code": "FAULT_INJECTED",
                                           "message": "Injected transient API error."}}, 503)
        if p == "/v1/stream" and "stream_disconnect" in af:
            return JSONResponse({"detail": {"code": "FAULT_INJECTED"}}, 503)
        resp = await call_next(request)
        if "stale_data" in af and request.method == "GET":
            resp.headers["X-Simulator-Stale"] = "true"
        return resp

    def nf():
        return HTTPException(404, {"code": "NOT_FOUND", "message": "unknown id"})

    @app.get("/v1/health")
    def health():
        return {"status": "ok", "database": "ok", "simulation": {"status": sim.status, "tick": sim.tick}}

    @app.get("/v1/instance")
    def instance():
        return dict(id=1, scenario_id="baseline", scenario_version="1.0", seed=sim.seed,
                    sim_time=sim.sim_time().isoformat(), tick=sim.tick,
                    tick_minutes=sim.tick_minutes, status=sim.status)

    @app.get("/v1/regions")
    def regions():
        return REGIONS

    @app.get("/v1/depots")
    def depots():
        return list(sim.depots.values())

    @app.get("/v1/depots/{eid}")
    def depot(eid: str):
        if eid not in sim.depots:
            raise nf()
        return sim.depots[eid]

    @app.get("/v1/stations")
    def stations():
        return list(sim.stations.values())

    @app.get("/v1/stations/{eid}")
    def station(eid: str):
        if eid not in sim.stations:
            raise nf()
        return sim.stations[eid]

    @app.get("/v1/routes")
    def routes():
        return list(sim.routes.values())

    @app.get("/v1/supply-arrivals")
    def arrivals():
        return sorted(sim.arrivals, key=lambda a: a["planned_tick"])

    @app.get("/v1/events")
    def events():
        return sorted(sim.events, key=lambda e: -e["id"])

    @app.get("/v1/allocations")
    def allocations():
        return sorted(sim.allocs, key=lambda a: -a["id"])

    @app.get("/v1/demand-history")
    def demand_history(station_id: str | None = None, limit: int = 200):
        limit = max(1, min(2000, limit))
        rows = [r for r in sim.demand if not station_id or r["station_id"] == station_id]
        return list(reversed(rows[-limit:]))

    @app.get("/v1/metrics")
    def metrics():
        served, unmet = sim.metrics["served"], sim.metrics["unmet"]
        act = [a for a in sim.allocs if a["status"] in ("IN_TRANSIT", "ARRIVED")]
        return dict(served_demand_liters=round(served, 3), unmet_demand_liters=round(unmet, 3),
                    service_level=round(served / (served + unmet), 6) if served + unmet else 1.0,
                    allocation_liters=sum(a["quantity"] for a in act),
                    allocation_failures=sum(a["status"] == "FAILED" for a in sim.allocs))

    def err(status, code, msg=""):
        return HTTPException(status, {"code": code, "message": msg or code})

    @app.post("/v1/allocations", status_code=201)
    def create_alloc(body: AllocIn):
        with sim.lock:
            same = next((a for a in sim.allocs if a["idempotency_key"] == body.idempotency_key), None)
            if same:
                keys = ("source_depot_id", "destination_station_id", "route_id", "fuel_type")
                if all(same[k] == getattr(body, k) for k in keys) and same["quantity"] == body.quantity:
                    return same
                raise err(409, "IDEMPOTENCY_KEY_MISMATCH")
            if body.fuel_type not in FUELS:
                raise HTTPException(422, [{"msg": "bad fuel_type"}])
            d, s, r = (sim.depots.get(body.source_depot_id), sim.stations.get(body.destination_station_id),
                       sim.routes.get(body.route_id))
            if not (d and s and r):
                raise err(404, "NOT_FOUND")
            if r["source_depot_id"] != d["id"] or r["destination_station_id"] != s["id"]:
                raise err(409, "ROUTE_MISMATCH")
            if d["status"] not in ("OPEN", "CONSTRAINED"):
                raise err(409, "DEPOT_CLOSED")
            if s["status"] != "OPEN":
                raise err(409, "STATION_CLOSED")
            if r["status"] != "AVAILABLE":
                raise err(409, "ROUTE_DISRUPTED")
            if body.quantity > r["max_shipment"]:
                raise err(409, "ROUTE_CAPACITY_EXCEEDED")
            if d["inventory"][body.fuel_type] < body.quantity:
                raise err(409, "INSUFFICIENT_INVENTORY")
            inflight = sum(a["quantity"] for a in sim.allocs if a["source_depot_id"] == d["id"] and
                           (a["status"] == "PENDING" or (a["status"] == "IN_TRANSIT" and a["departure_tick"] == sim.tick)))
            if inflight + body.quantity > d["dispatch_capacity_per_tick"]:
                raise err(409, "DISPATCH_CAPACITY_EXCEEDED")
            if s["inventory"][body.fuel_type] + body.quantity > s["capacity"][body.fuel_type]:
                raise err(409, "DESTINATION_CAPACITY_EXCEEDED")
            d["inventory"][body.fuel_type] -= body.quantity
            a = dict(id=len(sim.allocs) + 1, **body.model_dump(), created_tick=sim.tick,
                     departure_tick=None, expected_arrival_tick=None, actual_arrival_tick=None,
                     status="PENDING", failure_reason=None)
            sim.allocs.append(a)
            sim.log("allocation.created", "allocation", a["id"])
        sim.publish("allocation.status_changed", dict(a))
        return a

    @app.post("/v1/allocations/{aid}/cancel")
    def cancel(aid: int):
        with sim.lock:
            a = next((x for x in sim.allocs if x["id"] == aid), None)
            if not a:
                raise err(404, "ALLOCATION_NOT_FOUND")
            if a["status"] != "PENDING":
                raise err(409, "CANNOT_CANCEL")
            a["status"] = "CANCELLED"
            sim.depots[a["source_depot_id"]]["inventory"][a["fuel_type"]] += a["quantity"]
            sim.log("allocation.cancelled", "allocation", aid)
        sim.publish("allocation.status_changed", dict(a))
        return a

    @app.get("/v1/stream")
    async def stream():
        q: asyncio.Queue = asyncio.Queue(maxsize=200)
        sim.subscribers.append(q)

        async def gen():
            try:
                yield ": connected\n\n"
                while True:
                    try:
                        name, payload = await asyncio.wait_for(q.get(), 15)
                        yield f"event: {name}\ndata: {json.dumps(payload)}\n\n"
                    except asyncio.TimeoutError:
                        yield ": keepalive\n\n"
            finally:
                if q in sim.subscribers:
                    sim.subscribers.remove(q)
        return StreamingResponse(gen(), media_type="text/event-stream")

    # ---- admin -------------------------------------------------------------
    @app.post("/admin/run")
    def run():
        sim.status = "RUNNING"
        sim.log("admin.run")
        return {"status": "RUNNING"}

    @app.post("/admin/pause")
    def pause():
        sim.status = "PAUSED"
        sim.log("admin.pause")
        return {"status": "PAUSED"}

    @app.post("/admin/toggle")
    def toggle():
        sim.status = "PAUSED" if sim.status == "RUNNING" else "RUNNING"
        return {"status": sim.status}

    @app.post("/admin/step")
    def step():
        return sim.tick_once()

    @app.post("/admin/reset")
    def reset():
        sim.reset()
        return {"status": "reset"}

    @app.post("/admin/events", status_code=201)
    def add_event(body: EventIn):
        if body.type not in EVENT_TYPES:
            raise HTTPException(422, [{"msg": "bad type"}])
        with sim.lock:
            e = dict(id=len(sim.events) + 1, type=body.type, start_tick=body.start_tick,
                     end_tick=body.start_tick + body.duration_ticks, status="SCHEDULED",
                     parameters=body.parameters)
            sim.events.append(e)
            sim.log("event.created", "event", e["id"])
        return e

    @app.post("/admin/faults", status_code=201)
    def add_fault(body: FaultIn):
        if body.type not in FAULT_TYPES:
            raise HTTPException(422, [{"msg": "bad type"}])
        f = dict(id=len(sim.faults) + 1, type=body.type, parameters=body.parameters, active=True,
                 end=time.time() + body.duration_seconds)
        sim.faults.append(f)
        sim.log("fault.created", "fault", f["id"])
        return {k: v for k, v in f.items() if k != "end"}

    @app.post("/admin/faults/clear")
    def clear_faults():
        for f in sim.faults:
            f["active"] = False
        sim.log("fault.clear_all")
        return {"status": "cleared"}

    @app.get("/admin/audit")
    def get_audit(limit: int = 200):
        return list(reversed(sim.audit[-max(1, min(1000, limit)):]))

    @app.get("/admin/faults")
    def list_faults():
        return [{k: v for k, v in f.items() if k != "end"} for f in reversed(sim.faults[-50:])]

    @app.get("/admin/events")
    def list_events():
        return list(reversed(sim.events[-50:]))

    return app


app = build_app()
