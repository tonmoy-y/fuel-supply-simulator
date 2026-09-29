"""Structured JSON logging + Prometheus metrics (own registry so tests can build many apps)."""
from __future__ import annotations

import json
import logging
import time

from prometheus_client import CollectorRegistry, Counter, Gauge, Histogram, PlatformCollector, ProcessCollector


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        out = {"ts": round(time.time(), 3), "level": record.levelname, "logger": record.name,
               "msg": record.getMessage()}
        out.update(getattr(record, "ctx", {}))
        if record.exc_info:
            out["exc"] = self.formatException(record.exc_info)
        return json.dumps(out, default=str)


def setup_logging(level: str = "INFO") -> None:
    root = logging.getLogger()
    if any(getattr(h, "_fuelops", False) for h in root.handlers):
        return
    h = logging.StreamHandler()
    h.setFormatter(JsonFormatter())
    h._fuelops = True  # type: ignore[attr-defined]
    root.addHandler(h)
    root.setLevel(level)
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)


def log(logger: logging.Logger, level: int, msg: str, **ctx) -> None:
    logger.log(level, msg, extra={"ctx": ctx})


class Telemetry:
    def __init__(self) -> None:
        r = self.registry = CollectorRegistry()
        ProcessCollector(registry=r)   # CPU / memory / fds (Linux)
        PlatformCollector(registry=r)
        self.sim_requests = Counter("fuelops_sim_requests_total", "Simulator requests", ["endpoint", "method", "status"], registry=r)
        self.sim_latency = Histogram("fuelops_sim_request_seconds", "Simulator request latency", ["endpoint"],
                                     buckets=(.01, .025, .05, .1, .25, .5, 1, 2.5, 5), registry=r)
        self.sim_retries = Counter("fuelops_sim_retries_total", "Retries of transient simulator failures", registry=r)
        self.breaker_open = Gauge("fuelops_breaker_open", "1 when circuit breaker open", registry=r)
        self.stale_responses = Counter("fuelops_stale_responses_total", "Responses flagged X-Simulator-Stale", registry=r)
        self.data_age = Gauge("fuelops_data_age_seconds", "Age of newest fully-fresh snapshot", registry=r)
        self.mode = Gauge("fuelops_mode", "0=NORMAL 1=DEGRADED 2=OFFLINE", registry=r)
        self.sse_events = Counter("fuelops_sse_events_total", "SSE events received", ["event"], registry=r)
        self.sse_reconnects = Counter("fuelops_sse_reconnects_total", "SSE reconnect attempts", registry=r)
        self.recs = Counter("fuelops_recommendations_total", "Recommendations generated", ["severity"], registry=r)
        self.decisions = Counter("fuelops_decisions_total", "Operator/autopilot decisions", ["outcome"], registry=r)
        self.alloc_liters = Counter("fuelops_allocated_liters_total", "Liters accepted by simulator", registry=r)
        self.fallback = Counter("fuelops_fallback_activations_total", "Fallback policy activations", ["reason"], registry=r)
        self.forecast_mape = Gauge("fuelops_forecast_mape", "Rolling forecast MAPE (1-tick-ahead)", registry=r)
        self.model_conf = Gauge("fuelops_model_confidence_share_high", "Share of stations/fuels with high-confidence forecast", registry=r)
        self.shortage_alerts = Gauge("fuelops_shortage_alerts", "Current CRITICAL+HIGH stations/fuels", registry=r)
        self.incidents_open = Gauge("fuelops_incidents_open", "Open incidents", ["kind"], registry=r)
        self.engine_seconds = Histogram("fuelops_engine_seconds", "Decision engine runtime", registry=r,
                                        buckets=(.001, .005, .01, .025, .05, .1, .25, .5, 1))
        self.http_requests = Counter("fuelops_http_requests_total", "App HTTP requests", ["path", "method", "status"], registry=r)
        self.http_latency = Histogram("fuelops_http_request_seconds", "App HTTP latency", ["path"],
                                      buckets=(.005, .01, .025, .05, .1, .25, .5, 1, 2.5), registry=r)
