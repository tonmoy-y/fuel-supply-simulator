# Architecture
```
 Simulator (REST /v1/*, SSE /v1/stream)  <-- only write: POST /v1/allocations
        |  simclient.py: timeouts, retries+backoff, circuit breaker, stale-header, payload validation
        v
 service.py: refresh loop (REST truth) + SSE hint loop -> cached snapshot (degraded mode when stale)
        |                                  |
        v                                  v
 forecast.py (EWMA, anomalies)      incidents (crisis vs service_fault), decision ledger
        |
        v
 optimizer.py: LP alternative (scipy HiGHS), output re-validated
 engine.py: risk projection; simulate_allocation() what-if -> bisection sizing -> shared budgets -> validation (mirrors guide order)
        |          -> explainable recommendations ; fallback_policy when forecast unavailable/low confidence
        v
 app.py (FastAPI): /api/*, /healthz, /readyz, /metrics  -->  static/index.html operator dashboard
```
Principles: REST is truth; SSE advisory; permanent errors (404/409/422) never retried; transient 503 FAULT_INJECTED retried;
every write carries an idempotency key; stale data blocks execution; admin proxy off by default and never exposes reset.
