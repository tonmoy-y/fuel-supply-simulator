"""Adapter to the BUP Fuel Supply Simulator (Final Integration Guide).

Error taxonomy:
  SimTransient  - 503 FAULT_INJECTED, timeouts, connection errors  -> bounded retry w/ backoff
  SimPermanent  - 404/409/422 (carries simulator code)             -> NEVER retried
  SimInvalid    - 2xx with unparsable / wrong-shape body           -> rejected, alert raised
  CircuitOpen   - breaker open, fail fast
Only allocation creation carries an idempotency_key (in the JSON body, per guide), which
makes retrying POST /v1/allocations safe.
"""
from __future__ import annotations

import asyncio
import logging
import random
import time
from typing import Any, AsyncIterator

import httpx

from .config import Settings
from .telemetry import Telemetry, log

L = logging.getLogger("fuelops.sim")


class SimError(Exception):
    pass


class SimTransient(SimError):
    pass


class SimInvalid(SimError):
    pass


class CircuitOpen(SimTransient):
    pass


class SimPermanent(SimError):
    def __init__(self, status: int, code: str, message: str = ""):
        super().__init__(f"{status} {code}: {message}")
        self.status, self.code, self.message = status, code, message


def _endpoint_label(path: str) -> str:
    parts = [p for p in path.split("/") if p]
    out = []
    for p in parts:
        out.append(":id" if (p.isdigit() or p.startswith(("depot-", "station-", "route-"))) else p)
    return "/" + "/".join(out)


class SimClient:
    def __init__(self, settings: Settings, telemetry: Telemetry, transport: httpx.AsyncBaseTransport | None = None):
        self.s, self.t = settings, telemetry
        self._http = httpx.AsyncClient(base_url=settings.sim_base_url, timeout=settings.request_timeout_s,
                                       transport=transport)
        self.consecutive_failures = 0
        self.breaker_until = 0.0
        self.last_stale_at = 0.0          # wall time of last X-Simulator-Stale response
        self.last_ok_at = 0.0
        self.recent_latency: list[float] = []

    async def aclose(self) -> None:
        await self._http.aclose()

    # -- resilience helpers ---------------------------------------------------
    @property
    def breaker_is_open(self) -> bool:
        return time.time() < self.breaker_until

    def _record(self, ok: bool) -> None:
        if ok:
            self.consecutive_failures = 0
            self.last_ok_at = time.time()
        else:
            self.consecutive_failures += 1
            if self.consecutive_failures >= self.s.breaker_threshold:
                self.breaker_until = time.time() + self.s.breaker_cooldown_s
                self.t.breaker_open.set(1)
                log(L, logging.WARNING, "circuit breaker opened", failures=self.consecutive_failures)
        if ok and self.t.breaker_open._value.get():  # type: ignore[attr-defined]
            self.t.breaker_open.set(0)

    async def _request(self, method: str, path: str, *, retry: bool = True, **kw) -> tuple[Any, httpx.Response]:
        label = _endpoint_label(path)
        attempts = (self.s.max_retries + 1) if retry else 1
        last: Exception | None = None
        for i in range(attempts):
            if self.breaker_is_open:
                raise CircuitOpen("circuit breaker open")
            t0 = time.perf_counter()
            try:
                r = await self._http.request(method, path, **kw)
                dt = time.perf_counter() - t0
                self.t.sim_latency.labels(label).observe(dt)
                self.recent_latency = (self.recent_latency + [dt])[-50:]
                self.t.sim_requests.labels(label, method, str(r.status_code)).inc()
                if r.headers.get("X-Simulator-Stale", "").lower() == "true":
                    self.last_stale_at = time.time()
                    self.t.stale_responses.inc()
                if r.status_code == 503:
                    raise SimTransient(f"503 {self._code(r)}")
                if r.status_code in (404, 409, 422) or 400 <= r.status_code < 500:
                    self._record(True)   # service is up; request was the problem
                    code, msg = self._error_body(r)
                    raise SimPermanent(r.status_code, code, msg)
                if r.status_code >= 500:
                    raise SimTransient(f"{r.status_code}")
                try:
                    data = r.json()
                except ValueError as e:
                    self._record(False)
                    raise SimInvalid(f"non-JSON body from {label}") from e
                self._record(True)
                return data, r
            except SimPermanent:
                raise
            except SimInvalid:
                raise
            except (httpx.TimeoutException, httpx.TransportError, SimTransient) as e:
                if not isinstance(e, SimTransient):
                    self.t.sim_requests.labels(label, method, "error").inc()
                self._record(False)
                last = e if isinstance(e, SimError) else SimTransient(f"{type(e).__name__}: {e}")
                if i < attempts - 1:
                    self.t.sim_retries.inc()
                    await asyncio.sleep(self.s.backoff_base_s * (2 ** i) * (0.5 + random.random()))
        assert last is not None
        raise last if isinstance(last, SimError) else SimTransient(str(last))

    @staticmethod
    def _code(r: httpx.Response) -> str:
        try:
            j = r.json()
            return (j.get("error") or j.get("detail") or {}).get("code", "")
        except Exception:
            return ""

    @staticmethod
    def _error_body(r: httpx.Response) -> tuple[str, str]:
        try:
            j = r.json()
            d = j.get("detail", j)
            if isinstance(d, dict):
                return d.get("code", "ERROR"), d.get("message", "")
            if isinstance(d, list):     # FastAPI/Pydantic 422 shape
                return "VALIDATION_ERROR", str(d)[:300]
        except Exception:
            pass
        return "ERROR", r.text[:200]

    # -- validated reads ------------------------------------------------------
    async def get_list(self, path: str, **params) -> tuple[list[dict], bool]:
        data, r = await self._request("GET", path, params=params or None)
        if not isinstance(data, list):
            raise SimInvalid(f"{path}: expected list")
        return data, r.headers.get("X-Simulator-Stale", "").lower() == "true"

    async def get_obj(self, path: str) -> tuple[dict, bool]:
        data, r = await self._request("GET", path)
        if not isinstance(data, dict):
            raise SimInvalid(f"{path}: expected object")
        return data, r.headers.get("X-Simulator-Stale", "").lower() == "true"

    async def health(self) -> dict:
        data, _ = await self._request("GET", "/v1/health", retry=False)
        return data

    async def instance(self):
        return await self.get_obj("/v1/instance")

    async def depots(self):
        return await self.get_list("/v1/depots")

    async def stations(self):
        return await self.get_list("/v1/stations")

    async def routes(self):
        return await self.get_list("/v1/routes")

    async def arrivals(self):
        return await self.get_list("/v1/supply-arrivals")

    async def events(self):
        return await self.get_list("/v1/events")

    async def allocations(self):
        return await self.get_list("/v1/allocations")

    async def metrics(self):
        return await self.get_obj("/v1/metrics")

    async def demand_history(self, station_id: str, limit: int):
        return await self.get_list("/v1/demand-history", station_id=station_id, limit=max(1, min(2000, limit)))

    # -- the only domain write ------------------------------------------------
    async def create_allocation(self, body: dict) -> dict:
        """Retries only transient failures; same idempotency_key => safe replay (201)."""
        self._client_validate(body)
        data, _ = await self._request("POST", "/v1/allocations", json=body)
        if not isinstance(data, dict) or "id" not in data or "status" not in data:
            raise SimInvalid("allocation response missing id/status")
        return data

    async def cancel_allocation(self, alloc_id: int) -> dict:
        data, _ = await self._request("POST", f"/v1/allocations/{alloc_id}/cancel", retry=True)
        return data

    @staticmethod
    def _client_validate(b: dict) -> None:
        need = ("idempotency_key", "source_depot_id", "destination_station_id", "route_id", "fuel_type", "quantity")
        miss = [k for k in need if k not in b]
        if miss:
            raise SimPermanent(422, "CLIENT_VALIDATION", f"missing {miss}")
        if not (1 <= len(str(b["idempotency_key"])) <= 150):
            raise SimPermanent(422, "CLIENT_VALIDATION", "idempotency_key length 1-150")
        if b["fuel_type"] not in ("DIESEL", "PETROL", "OCTANE"):
            raise SimPermanent(422, "CLIENT_VALIDATION", "fuel_type enum")
        if not (isinstance(b["quantity"], (int, float)) and b["quantity"] > 0):
            raise SimPermanent(422, "CLIENT_VALIDATION", "quantity must be > 0")

    # -- SSE (advisory only) --------------------------------------------------
    async def stream(self) -> AsyncIterator[tuple[str, dict]]:
        """Yield (event, payload). Raises SimTransient on 503/disconnect so caller can reconnect+resync."""
        try:
            async with self._http.stream("GET", "/v1/stream", timeout=httpx.Timeout(5, read=40)) as r:
                if r.status_code != 200:
                    raise SimTransient(f"stream {r.status_code}")
                name = None
                async for line in r.aiter_lines():
                    if line.startswith(":"):
                        if line.strip() == ": connected":
                            yield "__connected__", {}
                        continue          # ': keepalive' - normal silence, not a disconnect
                    if line.startswith("event:"):
                        name = line[6:].strip()
                    elif line.startswith("data:") and name:
                        import json
                        try:
                            yield name, json.loads(line[5:].strip())
                        except ValueError:
                            pass
                        name = None
        except (httpx.TransportError, httpx.TimeoutException) as e:
            raise SimTransient(f"stream: {type(e).__name__}") from e
