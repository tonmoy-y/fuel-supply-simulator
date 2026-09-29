"""Client resilience tests against the MOCK simulator (contract tests)."""
from __future__ import annotations

import httpx
import pytest

from fuelops.config import Settings
from fuelops.simclient import CircuitOpen, SimClient, SimInvalid, SimPermanent, SimTransient
from fuelops.telemetry import Telemetry

BODY = dict(idempotency_key="k1", source_depot_id="depot-gazipur", destination_station_id="station-mirpur",
            route_id="route-gazipur-mirpur", fuel_type="DIESEL", quantity=3000)


async def inject(admin, type_, secs=30, **params):
    r = await admin.post("/admin/faults", json={"type": type_, "duration_seconds": secs, "parameters": params})
    assert r.status_code == 201


async def test_reads_validate_shape(client):
    depots, stale = await client.depots()
    assert len(depots) == 2 and stale is False
    assert (await client.instance())[0]["tick"] == 0


async def test_create_allocation_201_and_idempotent_replay(client, sim):
    a = await client.create_allocation(dict(BODY))
    b = await client.create_allocation(dict(BODY))          # same key + same body => safe replay
    assert a["id"] == b["id"] and len(sim.allocs) == 1


async def test_same_key_different_body_is_permanent_409_not_retried(client, transport):
    await client.create_allocation(dict(BODY))
    n = len(transport.calls)
    with pytest.raises(SimPermanent) as e:
        await client.create_allocation(dict(BODY, quantity=1000))
    assert e.value.status == 409 and e.value.code == "IDEMPOTENCY_KEY_MISMATCH"
    assert len(transport.calls) - n == 1                    # exactly one attempt: no retry of permanent errors


async def test_409_business_errors_never_retried(client, transport):
    with pytest.raises(SimPermanent) as e:
        await client.create_allocation(dict(BODY, quantity=9000, idempotency_key="k2"))
    assert e.value.code == "ROUTE_CAPACITY_EXCEEDED"
    assert sum(1 for c in transport.calls if c[0] == "POST") == 1


async def test_404_unknown_route(client):
    with pytest.raises(SimPermanent) as e:
        await client.create_allocation(dict(BODY, route_id="route-x", idempotency_key="k3"))
    assert e.value.status == 404 and e.value.code == "NOT_FOUND"


async def test_client_side_validation_blocks_bad_payload_before_network(client, transport):
    for bad in (dict(BODY, quantity=0), dict(BODY, fuel_type="KEROSENE"), {k: v for k, v in BODY.items() if k != "route_id"}):
        with pytest.raises(SimPermanent) as e:
            await client.create_allocation(bad)
        assert e.value.code == "CLIENT_VALIDATION"
    assert transport.calls == []


async def test_unavailable_fault_retried_then_raises_transient(client, admin, transport):
    await inject(admin, "unavailable")
    with pytest.raises(SimTransient):
        await client.depots()
    assert sum(1 for c in transport.calls if c[1] == "/v1/depots") == 3     # 1 + max_retries(2)


async def test_transient_error_rate_recovers_with_retries(admin, mock_app, settings, transport):
    import random
    random.seed(3)
    c = SimClient(Settings(**{**settings.__dict__, "max_retries": 8}), Telemetry(), transport=transport)
    await inject(admin, "error_rate", rate=0.5)
    ok = 0
    for _ in range(10):
        try:
            await c.stations()
            ok += 1
        except SimTransient:
            pass
    assert ok >= 8


async def test_post_retry_after_transient_is_safe_via_idempotency_key(client, admin, sim):
    await inject(admin, "unavailable", 1)
    with pytest.raises(SimTransient):
        await client.create_allocation(dict(BODY))
    await admin.post("/admin/faults/clear")
    a = await client.create_allocation(dict(BODY))
    assert a["status"] == "PENDING" and len(sim.allocs) == 1


async def test_stale_data_header_detected(client, admin):
    await inject(admin, "stale_data")
    _, stale = await client.stations()
    assert stale is True and client.last_stale_at > 0
    await admin.post("/admin/faults/clear")
    _, stale = await client.stations()
    assert stale is False


async def test_latency_fault_observable(client, admin):
    await inject(admin, "latency", delay_ms=120)
    await client.instance()
    assert client.recent_latency[-1] >= 0.11


async def test_health_bypasses_faults(client, admin):
    await inject(admin, "unavailable")
    assert (await client.health())["status"] == "ok"


async def test_circuit_breaker_opens_and_fails_fast(settings, transport, admin):
    c = SimClient(Settings(**{**settings.__dict__, "breaker_threshold": 3, "max_retries": 1}), Telemetry(), transport=transport)
    await inject(admin, "unavailable")
    with pytest.raises(SimTransient):
        await c.depots()
    with pytest.raises(SimTransient):
        await c.depots()
    n = len(transport.calls)
    with pytest.raises(CircuitOpen):
        await c.depots()
    assert len(transport.calls) == n                        # no network call while open


async def test_invalid_response_rejected(settings):
    def h(req):
        return httpx.Response(200, text="<html>not json</html>")
    c = SimClient(settings, Telemetry(), transport=httpx.MockTransport(h))
    with pytest.raises(SimInvalid):
        await c.depots()
    def h2(req):
        return httpx.Response(200, json={"oops": 1})
    c2 = SimClient(settings, Telemetry(), transport=httpx.MockTransport(h2))
    with pytest.raises(SimInvalid):
        await c2.depots()


async def test_sse_parser_handles_comments_events_and_keepalive(settings):
    body = (": connected\n\n: keepalive\n\nevent: simulation.tick\ndata: {\"tick\": 5}\n\n"
            "event: allocation.status_changed\ndata: {\"id\": 1}\n\n")
    c = SimClient(settings, Telemetry(), transport=httpx.MockTransport(lambda r: httpx.Response(200, text=body)))
    got = [x async for x in c.stream()]
    assert got == [("__connected__", {}), ("simulation.tick", {"tick": 5}), ("allocation.status_changed", {"id": 1})]


async def test_sse_503_fault_raises_transient(client, admin):
    await inject(admin, "stream_disconnect")
    with pytest.raises(SimTransient):
        async for _ in client.stream():
            pass
