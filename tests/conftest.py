"""Shared fixtures. The simulator here is the in-process MOCK built from the Integration Guide,
so every test that uses it is a CONTRACT/INTEGRATION-vs-MOCK test, not a real-simulator test."""
from __future__ import annotations

import httpx
import pytest

from fuelops.config import Settings
from fuelops.service import FuelOps
from fuelops.simclient import SimClient
from fuelops.telemetry import Telemetry
from mocksim.server import Sim, build_app


class CountingTransport(httpx.AsyncBaseTransport):
    def __init__(self, inner):
        self.inner, self.calls = inner, []

    async def handle_async_request(self, request):
        self.calls.append((request.method, request.url.path))
        return await self.inner.handle_async_request(request)


@pytest.fixture
def sim():
    return Sim(seed=12345)


@pytest.fixture
def settings():
    return Settings(sim_base_url="http://mock", backoff_base_s=0.001, max_retries=2, breaker_threshold=8,
                    breaker_cooldown_s=0.3, refresh_interval_s=0.05, stale_after_s=15, enable_sse=False)


@pytest.fixture
def mock_app(sim):
    return build_app(sim)


@pytest.fixture
def transport(mock_app):
    return CountingTransport(httpx.ASGITransport(app=mock_app))


@pytest.fixture
def client(settings, transport):
    return SimClient(settings, Telemetry(), transport=transport)


@pytest.fixture
def ops(settings, transport):
    t = Telemetry()
    return FuelOps(settings, SimClient(settings, t, transport=transport), t)


@pytest.fixture
async def admin(mock_app):
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=mock_app), base_url="http://mock") as c:
        yield c


async def step(admin, n=1):
    for _ in range(n):
        assert (await admin.post("/admin/step")).status_code == 200
