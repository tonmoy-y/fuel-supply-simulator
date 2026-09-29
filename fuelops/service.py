"""Orchestration: state sync/cache, analysis, incidents, decision ledger, SSE, autopilot.

Design rules (from the Integration Guide):
  * REST is the source of truth; SSE only triggers a re-fetch.
  * Failed sections keep last-known-good data but are flagged stale (never silently reused).
  * The only simulator write is POST /v1/allocations (+ documented cancel).
"""
from __future__ import annotations

import asyncio
import logging
import time
from datetime import datetime
from typing import Any

from .config import Settings
from .engine import World, assess_all, fallback_policy, make_budgets, recommend, validate_allocation
from .forecast import FUELS, Model, detect_anomalies, fit_model
from .simclient import SimClient, SimError, SimInvalid, SimPermanent, SimTransient
from .telemetry import Telemetry, log

L = logging.getLogger("fuelops.service")
SECTIONS = ("instance", "depots", "stations", "routes", "arrivals", "events", "allocations", "metrics")


class FuelOps:
    def __init__(self, settings: Settings, client: SimClient, telemetry: Telemetry):
        self.s, self.c, self.t = settings, client, telemetry
        self.data: dict[str, Any] = {k: None for k in SECTIONS}
        self.fetched_at: dict[str, float] = {k: 0.0 for k in SECTIONS}
        self.section_error: dict[str, str] = {}
        self.history: dict[str, list[dict]] = {}
        self.history_ok = False
        self.stale = False
        self.last_full_fresh = 0.0
        self.mode = "OFFLINE"
        self.policy_mode = "auto"            # 'auto' | 'fallback' (operator-forced: model unavailable)
        self.sse_connected = False
        self.analysis: dict[str, Any] = {"assessments": [], "recommendations": [], "anomalies": [], "engine": "none"}
        self.models: dict[tuple[str, str], Model] = {}
        self.rec_cache: dict[str, dict] = {}
        self.decisions: list[dict] = []
        self.deferred: list[dict] = []
        self.incidents: dict[str, dict] = {}
        self._clear_count: dict[str, int] = {}
        self._pred: dict[tuple[str, str, int], float] = {}
        self._ape: list[float] = []
        self._refresh_lock = asyncio.Lock()
        self._exec_lock = asyncio.Lock()
        self._trigger = asyncio.Event()
        self._tasks: list[asyncio.Task] = []
        self._running = False
        self.cycles = 0
        self.last_refresh_ms = 0.0

    # ------------------------------------------------------------ lifecycle
    async def start(self) -> None:
        self._running = True
        self._tasks = [asyncio.create_task(self._refresh_loop())]
        if self.s.enable_sse:
            self._tasks.append(asyncio.create_task(self._sse_loop()))

    async def stop(self) -> None:
        self._running = False
        for t in self._tasks:
            t.cancel()
        await asyncio.gather(*self._tasks, return_exceptions=True)
        await self.c.aclose()

    async def _refresh_loop(self) -> None:
        while self._running:
            try:
                await self.refresh()
                await self._retry_deferred()
                if self.s.autopilot:
                    await self._autopilot()
            except Exception:  # never let the loop die
                L.exception("refresh loop error")
            try:
                await asyncio.wait_for(self._trigger.wait(), self.s.refresh_interval_s)
                await asyncio.sleep(0.15)     # debounce bursts of SSE events
            except asyncio.TimeoutError:
                pass
            self._trigger.clear()

    async def _sse_loop(self) -> None:
        backoff = 1.0
        while self._running:
            try:
                async for name, _payload in self.c.stream():
                    if name == "__connected__":
                        self.sse_connected = True
                        backoff = 1.0
                        await self.refresh()   # no replay after reconnect -> resync via REST
                        continue
                    self.t.sse_events.labels(name).inc()
                    self._trigger.set()        # advisory only: re-GET REST
                self.sse_connected = False
            except (SimTransient, SimError) as e:
                self.sse_connected = False
                log(L, logging.WARNING, "sse disconnected", reason=str(e))
            except asyncio.CancelledError:
                raise
            except Exception:
                self.sse_connected = False
                L.exception("sse loop error")
            self.t.sse_reconnects.inc()
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, 15)

    # ------------------------------------------------------------ sync
    async def refresh(self) -> None:
        async with self._refresh_lock:
            t0 = time.perf_counter()
            calls = {"instance": self.c.instance, "depots": self.c.depots, "stations": self.c.stations,
                     "routes": self.c.routes, "arrivals": self.c.arrivals, "events": self.c.events,
                     "allocations": self.c.allocations, "metrics": self.c.metrics}
            res = await asyncio.gather(*(fn() for fn in calls.values()), return_exceptions=True)
            now, stale_any, failed = time.time(), False, 0
            for name, r in zip(calls, res):
                if isinstance(r, BaseException):
                    failed += 1
                    self.section_error[name] = f"{type(r).__name__}: {r}"
                    if isinstance(r, SimInvalid):
                        self._fault("invalid_response", True, f"{name}: {r}")
                    continue
                data, stale = r
                self.data[name], self.fetched_at[name] = data, now
                self.section_error.pop(name, None)
                stale_any |= stale
            st_list = self.data["stations"] or []
            if st_list:
                hres = await asyncio.gather(*(self.c.demand_history(s["id"], self.s.history_limit_per_station)
                                              for s in st_list), return_exceptions=True)
                hfail = 0
                for s, r in zip(st_list, hres):
                    if isinstance(r, BaseException):
                        hfail += 1
                        continue
                    rows, stale = r
                    self.history[s["id"]] = rows
                    stale_any |= stale
                self.history_ok = hfail == 0
                failed += 1 if hfail else 0
                if hfail:
                    self.section_error["demand_history"] = f"{hfail} station history calls failed"
                else:
                    self.section_error.pop("demand_history", None)
            self.stale = stale_any
            if failed == 0 and not stale_any:
                self.last_full_fresh = now
            age = now - self.last_full_fresh if self.last_full_fresh else float("inf")
            if not any(self.fetched_at.values()) or age > self.s.offline_after_s:
                self.mode = "OFFLINE"
            elif failed or stale_any or self.c.breaker_is_open or age > self.s.stale_after_s:
                self.mode = "DEGRADED"
            else:
                self.mode = "NORMAL"
            self.t.mode.set({"NORMAL": 0, "DEGRADED": 1, "OFFLINE": 2}[self.mode])
            self.t.data_age.set(min(age, 1e6))
            self._detect_service_faults(failed, stale_any)
            self._analyse()
            self._update_crisis_incidents()
            self.cycles += 1
            self.last_refresh_ms = (time.perf_counter() - t0) * 1000

    # ------------------------------------------------------------ analysis
    def world(self) -> World | None:
        d = self.data
        if not (d["instance"] and d["depots"] and d["stations"] and d["routes"]):
            return None
        inst = d["instance"]
        return World(tick=inst["tick"], sim_time=datetime.fromisoformat(inst["sim_time"]),
                     tick_minutes=inst["tick_minutes"], depots={x["id"]: x for x in d["depots"]},
                     stations={x["id"]: x for x in d["stations"]}, routes={x["id"]: x for x in d["routes"]},
                     allocations=d["allocations"] or [], arrivals=d["arrivals"] or [], events=d["events"] or [],
                     models=self.models, stale=self.stale or self.mode != "NORMAL")

    def _analyse(self) -> None:
        d = self.data
        if not (d["instance"] and d["stations"] and d["depots"] and d["routes"]):
            return
        t0 = time.perf_counter()
        inst = d["instance"]
        tm = inst["tick_minutes"]
        sim_time = datetime.fromisoformat(inst["sim_time"])
        forced_fb = self.policy_mode == "fallback"
        hist_usable = self.history_ok and not forced_fb
        anomalies, models = [], {}
        for st in d["stations"]:
            rows = self.history.get(st["id"], []) if hist_usable else []
            for f in FUELS:
                m = fit_model(st, f, rows, tm, stale=self.stale)
                models[(st["id"], f)] = m
                if rows:
                    anomalies += detect_anomalies(st, f, rows, m)
        self.models = models
        self._track_forecast_error(sim_time, inst["tick"], tm)
        w = self.world()
        assert w is not None
        w.models = models
        engine = "heuristic+forecast"
        try:
            if forced_fb:
                raise RuntimeError("policy forced to fallback by operator")
            recs = recommend(w, trigger=0.10, target=self.s.target_risk, min_ship=self.s.min_shipment_l,
                             horizon=self.s.horizon_ticks)
            if not hist_usable:
                self.t.fallback.labels("history_unavailable").inc()
                engine = "heuristic+static-prior"
        except Exception as e:
            self.t.fallback.labels("engine_error" if not forced_fb else "operator_forced").inc()
            log(L, logging.WARNING, "engine fallback", reason=str(e))
            recs = fallback_policy(w, min_ship=self.s.min_shipment_l)
            engine = "fallback-rule"
            self._fault("fallback_active", True, f"decision engine using fallback rule ({e})")
        else:
            self._fault("fallback_active", False, "")
        ass = assess_all(w)
        for r in recs:
            self.rec_cache[r["id"]] = r
            if r["id"] not in self._seen_recs():
                self.t.recs.labels(r["severity"]).inc()
        if len(self.rec_cache) > 300:
            for k in list(self.rec_cache)[:100]:
                self.rec_cache.pop(k, None)
        self._seen = set(self.rec_cache)
        self.t.shortage_alerts.set(sum(1 for a in ass if a.severity in ("CRITICAL", "HIGH")))
        self.t.model_conf.set(sum(m.confidence == "high" for m in models.values()) / max(1, len(models)))
        self.analysis = {"assessments": [a.__dict__ for a in ass], "recommendations": recs,
                         "anomalies": anomalies, "engine": engine}
        self.t.engine_seconds.observe(time.perf_counter() - t0)

    def _seen_recs(self) -> set:
        return getattr(self, "_seen", set())

    def _track_forecast_error(self, sim_time: datetime, tick: int, tm: int) -> None:
        for (sid, f, tk), pred in list(self._pred.items()):
            if tk > tick:
                continue
            row = next((r for r in self.history.get(sid, []) if r["fuel_type"] == f and r["tick"] == tk), None)
            if row and row["demand_liters"] > 0:
                self._ape = (self._ape + [abs(pred - row["demand_liters"]) / row["demand_liters"]])[-200:]
            self._pred.pop((sid, f, tk), None)
        if self._ape:
            self.t.forecast_mape.set(sum(self._ape) / len(self._ape))
        for (sid, f), m in self.models.items():
            self._pred[(sid, f, tick + 1)] = m.per_tick(sim_time, 1)

    # ------------------------------------------------------------ incidents
    def _open_incident(self, iid: str, kind: str, typ: str, detail: str, tick: int | None = None) -> dict:
        inc = self.incidents.get(iid)
        if inc and inc["status"] != "RECOVERED":
            inc["detail"] = detail or inc["detail"]
            return inc
        inc = dict(id=iid, kind=kind, type=typ, status="OPEN", opened_at=time.time(), opened_tick=self._tick(),
                   resolved_at=None, recovered_at=None, detail=detail, timeline=[(time.time(), "opened")])
        self.incidents[iid] = inc
        log(L, logging.WARNING, "incident opened", incident=iid, kind=kind, type=typ, detail=detail)
        return inc

    def _tick(self) -> int | None:
        return (self.data.get("instance") or {}).get("tick")

    def _fault(self, typ: str, active: bool, detail: str) -> None:
        """Service/API faults (kind='service_fault') - separate from simulated crises."""
        iid = f"svc-{typ}"
        if active:
            self._clear_count[typ] = 0
            self._open_incident(iid, "service_fault", typ, detail)
        else:
            inc = self.incidents.get(iid)
            if inc and inc["status"] == "OPEN":
                self._clear_count[typ] = self._clear_count.get(typ, 0) + 1
                if self._clear_count[typ] >= 2:     # hysteresis
                    inc.update(status="RECOVERED", resolved_at=time.time(), recovered_at=time.time())
                    inc["timeline"].append((time.time(), "recovered"))
                    log(L, logging.INFO, "incident recovered", incident=iid)

    def _detect_service_faults(self, failed: int, stale_any: bool) -> None:
        self._fault("api_unavailable", self.c.breaker_is_open or self.mode == "OFFLINE" or failed >= len(SECTIONS),
                    f"simulator unreachable ({self.section_error.get('instance', '')})")
        self._fault("api_errors", 0 < failed < len(SECTIONS), f"{failed} section(s) failing: "
                    f"{', '.join(list(self.section_error)[:4])}")
        self._fault("stale_data", stale_any, "X-Simulator-Stale: true - cache invalidated, actions gated")
        lat = sorted(self.c.recent_latency)
        p95 = lat[int(0.95 * (len(lat) - 1))] if lat else 0
        self._fault("api_latency", len(lat) >= 10 and p95 > 0.4, f"p95 simulator latency {p95 * 1000:.0f} ms")
        self._fault("invalid_response", False, "")
        if self.s.enable_sse:
            self._fault("sse_disconnected", not self.sse_connected and self.cycles > 2, "SSE stream down; polling REST")
        for kind in ("crisis", "service_fault"):
            self.t.incidents_open.labels(kind).set(sum(1 for i in self.incidents.values()
                                                       if i["kind"] == kind and i["status"] != "RECOVERED"))

    def _update_crisis_incidents(self) -> None:
        """Simulated supply-chain crises come from GET /v1/events (kind='crisis')."""
        hot = any(a["severity"] in ("CRITICAL", "HIGH") for a in self.analysis["assessments"])
        for e in self.data.get("events") or []:
            iid = f"evt-{e['id']}"
            params = ", ".join(f"{k}={v}" for k, v in (e.get("parameters") or {}).items())
            if e["status"] == "ACTIVE":
                self._open_incident(iid, "crisis", e["type"], f"{e['type']} ticks {e['start_tick']}-{e['end_tick']} {params}")
            elif e["status"] == "RESOLVED":
                inc = self.incidents.get(iid) or self._open_incident(iid, "crisis", e["type"], f"{e['type']} {params}")
                if inc["status"] == "OPEN":
                    inc.update(status="RECOVERING", resolved_at=time.time())
                    inc["timeline"].append((time.time(), "event resolved, monitoring recovery"))
                if inc["status"] == "RECOVERING" and not hot:
                    inc.update(status="RECOVERED", recovered_at=time.time())
                    inc["timeline"].append((time.time(), "no CRITICAL/HIGH shortage risk - recovered"))
                    log(L, logging.INFO, "crisis recovered", incident=iid)
        for kind in ("crisis",):
            self.t.incidents_open.labels(kind).set(sum(1 for i in self.incidents.values()
                                                       if i["kind"] == kind and i["status"] != "RECOVERED"))

    # ------------------------------------------------------------ decisions
    def _record(self, rec: dict, outcome: str, actor: str, detail: str = "", alloc: dict | None = None,
                code: str | None = None) -> dict:
        d = dict(ts=time.time(), tick=self._tick(), rec_id=rec["id"], actor=actor, outcome=outcome, detail=detail,
                 code=code, station_id=rec["station_id"], fuel_type=rec["fuel_type"], quantity=rec["quantity"],
                 route_id=rec["route_id"], source_depot_id=rec["source_depot_id"],
                 allocation_id=(alloc or {}).get("id"), headline=rec["explanation"]["headline"],
                 idempotency_key=rec.get("idempotency_key"))
        self.decisions.append(d)
        self.decisions = self.decisions[-500:]
        self.t.decisions.labels(outcome).inc()
        log(L, logging.INFO, "decision", **{k: v for k, v in d.items() if k != "headline"})
        return d

    async def execute(self, rec_id: str, actor: str = "operator", force: bool = False) -> dict:
        async with self._exec_lock:
            rec = self.rec_cache.get(rec_id)
            if not rec or not rec.get("route_id"):
                return {"ok": False, "outcome": "NOT_FOUND", "detail": "unknown or non-executable recommendation"}
            prior = next((d for d in self.decisions if d["rec_id"] == rec_id and d["outcome"] == "ACCEPTED"), None)
            if prior:
                return {"ok": True, "outcome": "ACCEPTED", "detail": "already submitted (idempotent)", "decision": prior}
            if self.c.breaker_is_open:
                d = self._record(rec, "DEFERRED", actor, "circuit breaker open - will retry", code="CIRCUIT_OPEN")
                self._defer(rec)
                return {"ok": False, "outcome": "DEFERRED", "decision": d}
            await self.refresh()                       # always validate against fresh state
            if self.stale and not force:
                d = self._record(rec, "REJECTED_LOCAL", actor, "data flagged stale; refusing to act (use force)",
                                 code="STALE_DATA")
                return {"ok": False, "outcome": "REJECTED_LOCAL", "decision": d}
            w = self.world()
            req = dict(idempotency_key=rec["idempotency_key"], source_depot_id=rec["source_depot_id"],
                       destination_station_id=rec["station_id"], route_id=rec["route_id"],
                       fuel_type=rec["fuel_type"], quantity=rec["quantity"])
            viol = validate_allocation(w, req, make_budgets(w)) if w else [{"code": "NO_STATE", "message": "no state"}]
            if viol:
                d = self._record(rec, "REJECTED_LOCAL", actor, viol[0]["message"], code=viol[0]["code"])
                return {"ok": False, "outcome": "REJECTED_LOCAL", "decision": d}
            try:
                alloc = await self.c.create_allocation(req)
            except SimPermanent as e:               # 404/409/422: never retried
                d = self._record(rec, "REJECTED_SIM", actor, e.message or str(e), code=e.code)
                await self.refresh()
                return {"ok": False, "outcome": "REJECTED_SIM", "decision": d}
            except (SimTransient, SimInvalid) as e:
                d = self._record(rec, "DEFERRED", actor, f"{type(e).__name__}: {e}", code="TRANSIENT")
                self._defer(rec)
                return {"ok": False, "outcome": "DEFERRED", "decision": d}
            self.t.alloc_liters.inc(rec["quantity"])
            if self.data["allocations"] is not None and all(a["id"] != alloc["id"] for a in self.data["allocations"]):
                self.data["allocations"] = [alloc] + self.data["allocations"]
            d = self._record(rec, "ACCEPTED", actor, "accepted by simulator", alloc=alloc)
            await self.refresh()
            return {"ok": True, "outcome": "ACCEPTED", "decision": d, "allocation": alloc}

    def dismiss(self, rec_id: str, actor: str, reason: str) -> dict | None:
        rec = self.rec_cache.get(rec_id)
        return self._record(rec, "DISMISSED", actor, reason or "operator dismissed") if rec else None

    def _defer(self, rec: dict) -> None:
        if not any(x["rec"]["id"] == rec["id"] for x in self.deferred):
            self.deferred.append({"rec": rec, "attempts": 0})

    async def _retry_deferred(self) -> None:
        for item in list(self.deferred):
            if item["attempts"] >= 5 or self.c.breaker_is_open:
                if item["attempts"] >= 5:
                    self.deferred.remove(item)
                continue
            item["attempts"] += 1
            res = await self.execute(item["rec"]["id"], actor="retry")
            if res["outcome"] != "DEFERRED":
                self.deferred.remove(item)

    async def _autopilot(self) -> None:
        """Only executes non-review recommendations while data is fresh and mode is NORMAL."""
        if self.mode != "NORMAL" or self.stale:
            return
        n = 0
        for rec in self.analysis["recommendations"]:
            if n >= self.s.autopilot_max_per_cycle:
                break
            if rec.get("requires_review") or not rec.get("route_id"):
                continue
            if any(d["rec_id"] == rec["id"] for d in self.decisions):
                continue
            await self.execute(rec["id"], actor="autopilot")
            n += 1

    # ------------------------------------------------------------ views
    def freshness(self) -> dict:
        now = time.time()
        age = now - self.last_full_fresh if self.last_full_fresh else None
        return dict(mode=self.mode, stale=self.stale, data_age_s=None if age is None else round(age, 1),
                    section_errors=self.section_error, breaker_open=self.c.breaker_is_open,
                    sse_connected=self.sse_connected, policy_mode=self.policy_mode, autopilot=self.s.autopilot,
                    sections={k: (round(now - v, 1) if v else None) for k, v in self.fetched_at.items()})

    def briefing(self) -> str:
        """Deterministic operator summary (no LLM): what is happening / likely / recommended."""
        a = self.analysis["assessments"]
        if not a:
            return "No simulator state yet."
        crit = [x for x in a if x["severity"] == "CRITICAL"]
        high = [x for x in a if x["severity"] == "HIGH"]
        ev = [e for e in (self.data.get("events") or []) if e["status"] == "ACTIVE"]
        m = self.data.get("metrics") or {}
        parts = [f"Tick {self._tick()}: service level {m.get('service_level', float('nan')):.1%}."]
        if ev:
            parts.append("Active crises: " + ", ".join(sorted({e['type'] for e in ev})) + ".")
        parts.append(f"{len(crit)} critical and {len(high)} high shortage risks." if (crit or high)
                     else "No critical or high shortage risks.")
        if crit or high:
            w0 = (crit + high)[0]
            parts.append(f"Most urgent: {w0['station_id']} {w0['fuel']} ({w0['risk']:.0%} risk"
                         + (f", ~{w0['hours_to_stockout']} h to stock-out" if w0['hours_to_stockout'] else "") + ").")
        recs = [r for r in self.analysis["recommendations"] if r.get("route_id")]
        parts.append(f"{len(recs)} allocation(s) recommended, {sum(r['requires_review'] for r in recs)} need review.")
        if self.mode != "NORMAL":
            parts.append(f"System is {self.mode}: decisions are gated until data is fresh.")
        return " ".join(parts)

    def forecast_series(self, station_id: str, fuel: str, n: int = 24) -> dict | None:
        m = self.models.get((station_id, fuel))
        inst = self.data.get("instance")
        if not m or not inst:
            return None
        sim_time = datetime.fromisoformat(inst["sim_time"])
        hist = sorted((r for r in self.history.get(station_id, []) if r["fuel_type"] == fuel), key=lambda r: r["tick"])[-48:]
        return dict(station_id=station_id, fuel=fuel, model=m.source, confidence=m.confidence, sigma_rel=m.sigma_rel,
                    history=[dict(tick=r["tick"], demand=r["demand_liters"], unmet=r["unmet_liters"]) for r in hist],
                    forecast=[dict(tick=inst["tick"] + k, demand=round(m.per_tick(sim_time, k), 2)) for k in range(1, n + 1)])
