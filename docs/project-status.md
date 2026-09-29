# Project status and known gaps
Done and verified (MOCK simulator only): app, engine, LP optimizer, what-if simulate, forecast, client resilience, 71 tests, mutation checks, 14-step demo, benchmark (none/rule/engine/LP), load test, lint, evaluate.py harness, prompt audit.
Not verified / not built:
1. Never run against the official simulator image; the mock approximates demand noise and supply schedule.
2. Docker build, docker compose, GitHub Actions never executed (no Docker in build sandbox).
3. Engine and LP thresholds untuned for the real simulator.
4. Brief scan is missing printed page 8 (§15-17, start of §18): requirements there are untraced.
5. No generative-AI layer (briefing is deterministic text); no RL; no Kubernetes/Helm.
6. Dashboard (incl. Simulate button) JS syntax-checked only; never opened in a browser.
7. Engine ties the threshold rule on service level; its advantage is ~25-35% less fuel. LP is not dominant.
First step on your machine: `docker compose up -d --build`, `python scripts/demo.py`, `python scripts/evaluate.py --live`, then fix what differs.
