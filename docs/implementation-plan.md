# Implementation plan (as executed)
1 Audit sources (brief OCR, guide) → 2 mock simulator from guide → 3 simclient (retries, breaker, stale) → 4 forecast/anomaly → 5 engine + validator → 6 service (incidents, ledger, autopilot) → 7 API + dashboard → 8 tests + mutation checks → 9 demo + benchmark + load test → 10 LP optimizer + what-if simulate (this session) → 11 evaluate.py + docs.
Next (needs the user's machine): docker compose up against the official image, run `scripts/demo.py`, tune thresholds.
