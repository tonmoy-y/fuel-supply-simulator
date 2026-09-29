from __future__ import annotations

import httpx
import pytest

from fuelops.app import create_app
from fuelops.config import Settings
from tests.conftest import step


@pytest.fixture
async def api(settings, transport, admin):
    s = Settings(**{**settings.__dict__, "operator_token": "s3cret"})
    app = create_app(s, transport=transport, start_background=False)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://app") as c:
        c.app = app
        yield c
    await app.state.ops.c.aclose()


async def test_liveness_independent_of_simulator(api, admin):
    await admin.post("/admin/faults", json={"type": "unavailable", "duration_seconds": 30})
    assert (await api.get("/healthz")).json()["status"] == "ok"


async def test_readyz_503_before_first_sync_then_200(api, admin):
    assert (await api.get("/readyz")).status_code == 503
    await api.app.state.ops.refresh()
    r = await api.get("/readyz")
    assert r.status_code == 200 and r.json()["mode"] == "NORMAL"


async def test_state_recommendations_and_dashboard(api, admin, sim):
    await step(admin, 30)
    await api.app.state.ops.refresh()
    st = (await api.get("/api/state")).json()
    assert st["instance"]["tick"] == 30 and len(st["stations"]) == 4 and len(st["assessments"]) == 12
    assert "Tick 30" in st["briefing"]
    assert (await api.get("/api/recommendations")).status_code == 200
    html = (await api.get("/")).text
    assert "Fuel Ops Center" in html
    f = (await api.get("/api/forecast/station-mirpur/diesel")).json()
    assert len(f["forecast"]) == 24 and f["history"]


async def test_mutations_require_operator_token(api, admin, sim):
    await step(admin, 30)
    sim.stations["station-tongi"]["inventory"]["DIESEL"] = 900.0
    ops = api.app.state.ops
    await ops.refresh()
    rid = next(r["id"] for r in ops.analysis["recommendations"] if r["route_id"])
    assert (await api.post(f"/api/recommendations/{rid}/execute", json={})).status_code == 401
    assert (await api.post(f"/api/recommendations/{rid}/execute", json={}, headers={"X-Operator-Token": "wrong"})).status_code == 401
    r = await api.post(f"/api/recommendations/{rid}/execute", json={"actor": "op1"}, headers={"X-Operator-Token": "s3cret"})
    assert r.status_code == 200 and r.json()["outcome"] == "ACCEPTED"
    d = (await api.get("/api/decisions")).json()
    assert d[0]["actor"] == "op1" and d[0]["outcome"] == "ACCEPTED"


async def test_unknown_recommendation_404_and_input_validation(api):
    h = {"X-Operator-Token": "s3cret"}
    assert (await api.post("/api/recommendations/rec-nope/execute", json={}, headers=h)).status_code == 404
    assert (await api.post("/api/policy", json={"mode": "chaos"}, headers=h)).status_code == 422


async def test_admin_proxy_disabled_by_default_and_no_reset_exposed(api):
    h = {"X-Operator-Token": "s3cret"}
    assert (await api.post("/api/admin/step", headers=h)).status_code == 403
    assert (await api.post("/api/admin/reset", headers=h)).status_code == 404


async def test_metrics_endpoint_exposes_required_series(api, admin):
    await api.app.state.ops.refresh()
    await api.get("/api/state")
    text = (await api.get("/metrics")).text
    for name in ("fuelops_sim_requests_total", "fuelops_sim_request_seconds_bucket", "fuelops_data_age_seconds",
                 "fuelops_mode", "fuelops_decisions_total", "fuelops_fallback_activations_total",
                 "fuelops_forecast_mape", "fuelops_shortage_alerts", "fuelops_http_requests_total", "process_cpu_seconds_total"):
        assert name in text, name


async def test_service_health_separates_app_from_simulator(api, admin):
    await api.app.state.ops.refresh()
    h = (await api.get("/api/health/services")).json()
    assert h["app"]["status"] == "ok" and h["simulator_liveness"]["status"] == "ok" and "intelligence" in h


async def test_simulate_endpoint_is_read_only_and_validates(api, admin, transport):
    for _ in range(3):
        await step(admin)
    await api.app.state.ops.refresh()
    before = [c for c in transport.calls if c[0] == "POST" and c[1] == "/v1/allocations"]
    ops = api.app.state.ops
    w = ops.world()
    r = next(iter(w.routes.values()))
    body = dict(source_depot_id=r["source_depot_id"], destination_station_id=r["destination_station_id"],
                route_id=r["id"], fuel_type="DIESEL", quantity=1000)
    out = (await api.post("/api/simulate", json=body)).json()
    assert "before" in out and "after" in out and out["disclaimer"]
    bad = (await api.post("/api/simulate", json={**body, "quantity": 9e9})).status_code
    assert bad == 422
    assert [c for c in transport.calls if c[0] == "POST" and c[1] == "/v1/allocations"] == before   # no write
