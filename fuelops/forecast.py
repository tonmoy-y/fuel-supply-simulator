"""Demand forecasting + anomaly detection.

Method (deliberately simple, transparent, measurable):
  * Documented hour-of-day factors (guide 8.6) de-seasonalise observed per-tick demand.
  * Two EWMAs on the de-seasonalised series: slow (alpha .10) and fast (alpha .50).
    If fast/slow > SPIKE_RATIO the fast level is used (regime change / demand spike).
  * Station demand_multiplier (live REST field) gives a floor: level >= prior * multiplier.
  * Uncertainty = max(profile noise, residual std) -> horizon std grows with sqrt(n).
  * Fallback: if < MIN_OBS observations (or history unavailable) use the documented daily
    profile rate (source='static-prior', confidence='low').
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import datetime

FUELS = ("DIESEL", "PETROL", "OCTANE")
DAILY = {  # documented in guide 8.5: liters/day (DIESEL, PETROL, OCTANE), noise
    "urban_high": (8500, 10500, 5600, 0.10),
    "industrial": (14000, 4500, 2200, 0.08),
    "highway": (10500, 11000, 6200, 0.12),
    "regional": (7200, 7600, 3600, 0.10),
}
REGION_FACTOR = {"region-dhaka": 1.0, "region-chattogram": 1.08}
MIN_OBS = 6
SPIKE_RATIO = 1.25


def hour_factor(profile: str, hour: int) -> float:
    if profile == "industrial":
        return 1.55 if 6 <= hour <= 17 else 0.45
    if profile == "highway":
        return 1.35 if (6 <= hour <= 9 or 16 <= hour <= 20) else 0.75
    if profile == "urban_high":
        return 1.45 if (7 <= hour <= 9 or 16 <= hour <= 20) else 0.70
    return 1.25 if 7 <= hour <= 20 else 0.65


def norm_cdf(x: float) -> float:
    return 0.5 * (1 + math.erf(x / math.sqrt(2)))


@dataclass
class Model:
    station_id: str
    fuel: str
    profile: str
    level: float            # de-seasonalised liters per tick
    sigma_rel: float
    n_obs: int
    source: str             # 'ewma' | 'static-prior'
    spike: bool
    confidence: str         # high | medium | low
    tick_minutes: int = 15

    def per_tick(self, sim_time: datetime, k: int) -> float:
        """Forecast demand (L) for tick k steps ahead (k>=1)."""
        from datetime import timedelta
        t = sim_time + timedelta(minutes=self.tick_minutes * k)
        return self.level * hour_factor(self.profile, t.hour)


def prior_level(profile: str, fuel: str, region_id: str, tick_minutes: int, multiplier: float = 1.0) -> float:
    d = DAILY[profile][FUELS.index(fuel)]
    return d / 24 * (tick_minutes / 60) * REGION_FACTOR.get(region_id, 1.0) * multiplier


def fit_model(station: dict, fuel: str, rows: list[dict], tick_minutes: int, stale: bool = False) -> Model:
    """rows: demand-history rows for this station+fuel (any order)."""
    prof, rid = station["demand_profile"], station["region_id"]
    mult = float(station.get("demand_multiplier", 1.0))
    noise = DAILY[prof][3]
    prior = prior_level(prof, fuel, rid, tick_minutes, 1.0)
    obs = sorted((r for r in rows if r.get("fuel_type") == fuel), key=lambda r: r["tick"])
    series = []
    for r in obs:
        try:
            hr = datetime.fromisoformat(r["sim_time"]).hour
            series.append(float(r["demand_liters"]) / hour_factor(prof, hr))
        except (KeyError, ValueError, TypeError):
            continue
    if len(series) < MIN_OBS:
        lvl = prior * mult
        return Model(station["id"], fuel, prof, lvl, max(noise, 0.15), len(series), "static-prior",
                     mult > 1.05, "low", tick_minutes)
    slow = fast = series[0]
    for x in series[1:]:
        slow += 0.10 * (x - slow)
        fast += 0.50 * (x - fast)
    spike = fast / slow > SPIKE_RATIO if slow > 0 else False
    level = fast if spike else slow
    if mult > 1.05:                       # live signal from REST station state
        level = max(level, prior * mult * 0.9)
        spike = True
    resid = [abs(x - level) / level for x in series[-24:]] if level > 0 else [noise]
    sigma = max(noise, math.sqrt(sum(r * r for r in resid) / len(resid)))
    conf = "high" if len(series) >= 24 and not stale else ("medium" if not stale else "low")
    return Model(station["id"], fuel, prof, level, sigma, len(series), "ewma", spike, conf, tick_minutes)


def detect_anomalies(station: dict, fuel: str, rows: list[dict], model: Model) -> list[dict]:
    """Anomalous demand: last 2 ticks both > 1.4x the slow (de-seasonalised) expectation."""
    obs = sorted((r for r in rows if r.get("fuel_type") == fuel), key=lambda r: r["tick"])[-2:]
    if len(obs) < 2 or model.source != "ewma":
        return []
    prof = station["demand_profile"]
    ratios = []
    for r in obs:
        hr = datetime.fromisoformat(r["sim_time"]).hour
        exp = prior_level(prof, fuel, station["region_id"], model.tick_minutes) * hour_factor(prof, hr)
        ratios.append(float(r["demand_liters"]) / exp if exp else 1.0)
    if min(ratios) > 1.4:
        return [{"kind": "anomalous_demand", "station_id": station["id"], "fuel": fuel,
                 "detail": f"demand {min(ratios):.2f}x documented profile for 2 consecutive ticks"}]
    return []
