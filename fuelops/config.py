"""Environment-driven configuration (no secrets are required or stored)."""
from __future__ import annotations

import os
from dataclasses import dataclass, field


def _f(name: str, default: str) -> str:
    return os.environ.get(name, default)


@dataclass(frozen=True)
class Settings:
    sim_base_url: str = field(default_factory=lambda: _f("SIM_BASE_URL", "http://localhost:8000").rstrip("/"))
    request_timeout_s: float = field(default_factory=lambda: float(_f("SIM_TIMEOUT_S", "3")))
    max_retries: int = field(default_factory=lambda: int(_f("SIM_MAX_RETRIES", "3")))
    backoff_base_s: float = field(default_factory=lambda: float(_f("SIM_BACKOFF_BASE_S", "0.15")))
    breaker_threshold: int = field(default_factory=lambda: int(_f("SIM_BREAKER_THRESHOLD", "6")))
    breaker_cooldown_s: float = field(default_factory=lambda: float(_f("SIM_BREAKER_COOLDOWN_S", "5")))
    refresh_interval_s: float = field(default_factory=lambda: float(_f("REFRESH_INTERVAL_S", "2")))
    stale_after_s: float = field(default_factory=lambda: float(_f("STALE_AFTER_S", "15")))
    offline_after_s: float = field(default_factory=lambda: float(_f("OFFLINE_AFTER_S", "60")))
    history_limit_per_station: int = field(default_factory=lambda: int(_f("HISTORY_LIMIT", "240")))
    horizon_ticks: int = field(default_factory=lambda: int(_f("HORIZON_TICKS", "24")))
    target_risk: float = field(default_factory=lambda: float(_f("TARGET_RISK", "0.05")))
    min_shipment_l: float = field(default_factory=lambda: float(_f("MIN_SHIPMENT_L", "500")))
    autopilot: bool = field(default_factory=lambda: _f("AUTOPILOT", "false").lower() == "true")
    autopilot_max_per_cycle: int = field(default_factory=lambda: int(_f("AUTOPILOT_MAX_PER_CYCLE", "3")))
    enable_admin_proxy: bool = field(default_factory=lambda: _f("ENABLE_ADMIN_PROXY", "false").lower() == "true")
    operator_token: str = field(default_factory=lambda: _f("OPERATOR_TOKEN", ""))
    enable_sse: bool = field(default_factory=lambda: _f("ENABLE_SSE", "true").lower() == "true")
    log_level: str = field(default_factory=lambda: _f("LOG_LEVEL", "INFO"))
