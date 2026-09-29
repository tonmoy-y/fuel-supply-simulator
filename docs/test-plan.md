# Test plan and results (ALL against the MOCK simulator)
| Layer | Evidence | Result |
|---|---|---|
| Unit: forecast/engine | tests/test_forecast_engine.py | pass |
| Client: retries, breaker, stale, SSE parse | tests/test_simclient.py | pass |
| Service flows: execute, idempotent replay, deferred retry, incidents | tests/test_service_flows.py | pass |
| API | tests/test_app_api.py | pass |
| LP optimizer + what-if | tests/test_optimizer.py, test_app_api simulate | pass |
| Total | `pytest` | 71 passed |
| Mutation checks | engine/client: 3 injected bugs caught, 1 equivalent. LP: route filter, dispatch row, inventory row caught; validator removal equivalent | see left |
| Demo | scripts/demo.py, 14 steps (adds simulate what-if), latest run | all steps OK; final mock service level 0.908 |
| Benchmark | 192 ticks, 4 scenarios, none/rule/engine | see docs/evidence/benchmark.json |
| Load | 25 users, 15 s, app read APIs, current code | 331 rps, p50 15 ms, p95 63 ms, p99 110 ms, 0 errors; harness reruns 284-325 rps, p95 63-92 ms |
Not run: real simulator, Docker build/compose, GitHub Actions.

Harness: `python scripts/evaluate.py --live --bench --load` -> 12 passed, 0 failed, 0 skipped (mock).
