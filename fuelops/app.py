"""FastAPI backend + operator dashboard host."""
from __future__ import annotations

import logging
import time
from contextlib import asynccontextmanager
from pathlib import Path

import httpx
from fastapi import Depends, FastAPI, Header, HTTPException, Request, Response
from fastapi.responses import FileResponse
from prometheus_client import CONTENT_TYPE_LATEST, generate_latest
from pydantic import BaseModel, Field

from .config import Settings
from .service import FuelOps
from .simclient import SimClient
from .telemetry import Telemetry, log, setup_logging

L = logging.getLogger("fuelops.app")
STATIC = Path(__file__).parent / "static"


class ExecuteIn(BaseModel):
    actor: str = Field(default="operator", max_length=60)
    force: bool = False


class DismissIn(BaseModel):
    actor: str = Field(default="operator", max_length=60)
    reason: str = Field(default="", max_length=300)


class SimulateIn(BaseModel):
    source_depot_id: str
    destination_station_id: str
    route_id: str
    fuel_type: str = Field(pattern="^(DIESEL|PETROL|OCTANE)$")
    quantity: float = Field(gt=0, le=1_000_000)


class PolicyIn(BaseModel):
    mode: str = Field(pattern="^(auto|fallback)$")


def create_app(settings: Settings | None = None, transport: httpx.AsyncBaseTransport | None = None,
               start_background: bool = True) -> FastAPI:
    settings = settings or Settings()
    setup_logging(settings.log_level)
    telemetry = Telemetry()
    client = SimClient(settings, telemetry, transport=transport)
    ops = FuelOps(settings, client, telemetry)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        if start_background:
            await ops.start()
        log(L, logging.INFO, "app started", sim=settings.sim_base_url, autopilot=settings.autopilot)
        yield
        await ops.stop()

    app = FastAPI(title="Fuel Supply Intelligence & Resilience Platform", version="1.0.0", lifespan=lifespan)
    app.state.ops, app.state.telemetry, app.state.settings = ops, telemetry, settings

    def require_operator(x_operator_token: str | None = Header(default=None)) -> None:
        if settings.operator_token and x_operator_token != settings.operator_token:
            raise HTTPException(401, {"code": "UNAUTHORIZED", "message": "missing/invalid X-Operator-Token"})

    @app.middleware("http")
    async def metrics_mw(request: Request, call_next):
        t0 = time.perf_counter()
        try:
            resp = await call_next(request)
        except Exception:
            L.exception("unhandled error")
            telemetry.http_requests.labels(request.url.path, request.method, "500").inc()
            return Response('{"detail":{"code":"INTERNAL","message":"internal error"}}', 500,
                            media_type="application/json")
        route = request.scope.get("route")
        path = getattr(route, "path", request.url.path)
        telemetry.http_requests.labels(path, request.method, str(resp.status_code)).inc()
        telemetry.http_latency.labels(path).observe(time.perf_counter() - t0)
        return resp

    # ---------------------------------------------------------------- health
    @app.get("/healthz")
    async def healthz():
        """Application liveness only (independent of the simulator)."""
        return {"status": "ok", "cycles": ops.cycles}

    @app.get("/readyz")
    async def readyz(response: Response):
        """Readiness = fresh simulator data + not OFFLINE. /v1/health alone is NOT proof (bypasses faults)."""
        f = ops.freshness()
        ready = ops.mode != "OFFLINE" and ops.cycles > 0
        if not ready:
            response.status_code = 503
        return {"ready": ready, **f}

    @app.get("/metrics")
    async def metrics():
        return Response(generate_latest(telemetry.registry), media_type=CONTENT_TYPE_LATEST)

    _probe: dict = {"t": 0.0, "v": None}

    @app.get("/api/health/services")
    async def service_health():
        now = time.time()
        if now - _probe["t"] > 2.0:                 # cache: dashboard load must not become simulator load
            try:
                _probe["v"] = await client.health()
            except Exception as e:  # noqa: BLE001
                _probe["v"] = {"status": "unreachable", "error": str(e)[:120]}
            _probe["t"] = now
        sim = _probe["v"]
        lat = sorted(client.recent_latency)
        return dict(app={"status": "ok", "cycles": ops.cycles, "last_refresh_ms": round(ops.last_refresh_ms, 1)},
                    simulator_liveness=sim, freshness=ops.freshness(),
                    intelligence=dict(engine=ops.analysis["engine"], policy_mode=ops.policy_mode,
                                      mape=(sum(ops._ape) / len(ops._ape)) if ops._ape else None,
                                      high_confidence_share=(sum(m.confidence == "high" for m in ops.models.values())
                                                             / max(1, len(ops.models)))),
                    simulator_latency_ms=dict(p50=round(1000 * lat[len(lat) // 2], 1) if lat else None,
                                              p95=round(1000 * lat[int(.95 * (len(lat) - 1))], 1) if lat else None))

    # ---------------------------------------------------------------- state
    @app.get("/api/state")
    async def state():
        d = ops.data
        return dict(
            freshness=ops.freshness(), briefing=ops.briefing(), instance=d["instance"], metrics=d["metrics"],
            depots=d["depots"] or [], stations=d["stations"] or [], routes=d["routes"] or [],
            arrivals=[a for a in (d["arrivals"] or []) if a["status"] != "ARRIVED"][:12],
            events=d["events"] or [], assessments=ops.analysis["assessments"], anomalies=ops.analysis["anomalies"],
            engine=ops.analysis["engine"], allocations=(d["allocations"] or [])[:30],
            incidents=sorted(ops.incidents.values(), key=lambda i: -i["opened_at"])[:30],
        )

    @app.get("/api/recommendations")
    async def recommendations():
        return {"engine": ops.analysis["engine"], "items": ops.analysis["recommendations"]}

    @app.post("/api/recommendations/{rec_id}/execute", dependencies=[Depends(require_operator)])
    async def execute(rec_id: str, body: ExecuteIn):
        res = await ops.execute(rec_id, actor=body.actor, force=body.force)
        if res["outcome"] == "NOT_FOUND":
            raise HTTPException(404, {"code": "NOT_FOUND", "message": res["detail"]})
        return res

    @app.post("/api/recommendations/{rec_id}/dismiss", dependencies=[Depends(require_operator)])
    async def dismiss(rec_id: str, body: DismissIn):
        d = ops.dismiss(rec_id, body.actor, body.reason)
        if not d:
            raise HTTPException(404, {"code": "NOT_FOUND", "message": "unknown recommendation"})
        return d

    @app.post("/api/simulate")
    async def simulate(body: SimulateIn):
        """Read-only what-if: validate + project a candidate allocation (no simulator write)."""
        w = ops.world()
        if w is None:
            raise HTTPException(503, {"code": "NO_STATE", "message": "no simulator state yet"})
        from .engine import simulate_allocation
        return simulate_allocation(w, body.model_dump())

    @app.get("/api/decisions")
    async def decisions():
        return list(reversed(ops.decisions[-100:]))

    @app.get("/api/forecast/{station_id}/{fuel}")
    async def forecast(station_id: str, fuel: str):
        s = ops.forecast_series(station_id, fuel.upper())
        if not s:
            raise HTTPException(404, {"code": "NOT_FOUND", "message": "no model yet"})
        return s

    @app.post("/api/policy", dependencies=[Depends(require_operator)])
    async def set_policy(body: PolicyIn):
        """Operator switch: 'fallback' simulates 'ML model unavailable' -> rule-based policy."""
        ops.policy_mode = body.mode
        await ops.refresh()
        log(L, logging.WARNING, "policy mode changed", mode=body.mode)
        return {"policy_mode": ops.policy_mode, "engine": ops.analysis["engine"]}

    # Optional pass-through of NON-destructive sim controls for demo (disabled by default; no reset exposed)
    for action in ("run", "pause", "step"):
        def _mk(a: str):
            async def _h():
                if not settings.enable_admin_proxy:
                    raise HTTPException(403, {"code": "DISABLED", "message": "set ENABLE_ADMIN_PROXY=true"})
                async with httpx.AsyncClient(base_url=settings.sim_base_url, timeout=5) as c:
                    r = await c.post(f"/admin/{a}")
                await ops.refresh()
                return r.json()
            return _h
        app.post(f"/api/admin/{action}", dependencies=[Depends(require_operator)])(_mk(action))

    @app.get("/")
    async def index():
        return FileResponse(STATIC / "index.html")

    return app


def app_factory() -> FastAPI:  # uvicorn --factory fuelops.app:app_factory
    return create_app()
