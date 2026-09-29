# Evaluation plan and results
Run: `python scripts/evaluate.py --live --bench --load` (exit 1 on any required FAIL; SKIPPED = dependency unavailable). Report: `docs/evidence/evaluate-report.json`.
**Every number below comes from the MOCK simulator (`mocksim/`), not the official image.**

## Last measured run (this sandbox)
S1-S5 structural checks PASS; U1 pytest 71 passed; U2 ruff clean; O1 LP/engine/rule comparison ran; L1/L2 GET-only checks against `http://localhost:8000` (the mock) PASS; B1 0 allocation failures in every policy/scenario; P1 load test 25 users, 10 s: 284 rps, p95 91.5 ms, 0 errors (earlier run of the same harness: 325 rps, p95 62.7 ms; the sandbox is noisy, so treat these as ranges).
Negative test: removing `Dockerfile` makes S1 FAIL and the script exit 1 (then restored).

## Policy benchmark (192 ticks, seed 12345, identical injected events per policy)
| Scenario | Policy | Service level | Unmet L | Shipped L | Alloc failures | Mean decision ms |
|---|---|---|---|---|---|---|
| baseline | none | 0.4610 | 100,425 | 0 | 0 | 0.0 |
| baseline | rule | 1.0000 | 0 | 187,500 | 0 | 0.03 |
| baseline | engine | 1.0000 | 0 | 124,100 | 0 | 1.45 |
| baseline | lp | 0.9977 | 430 | 113,600 | 0 | 4.32 |
| demand_spike_dhaka_2.6x | none | 0.4171 | 120,064 | 0 | 0 | 0.0 |
| demand_spike_dhaka_2.6x | rule | 1.0000 | 0 | 207,600 | 0 | 0.04 |
| demand_spike_dhaka_2.6x | engine | 1.0000 | 0 | 144,200 | 0 | 1.71 |
| demand_spike_dhaka_2.6x | lp | 0.9986 | 297 | 133,700 | 0 | 4.39 |
| spike+route_disruption | none | 0.3990 | 129,380 | 0 | 0 | 0.0 |
| spike+route_disruption | rule | 1.0000 | 0 | 228,400 | 0 | 0.03 |
| spike+route_disruption | engine | 1.0000 | 0 | 157,000 | 0 | 1.21 |
| spike+route_disruption | lp | 0.9979 | 454 | 142,400 | 0 | 3.99 |
| combined_crisis | none | 0.3903 | 134,206 | 0 | 0 | 0.0 |
| combined_crisis | rule | 0.9865 | 2,979 | 219,300 | 0 | 0.04 |
| combined_crisis | engine | 0.9865 | 2,979 | 164,100 | 0 | 1.55 |
| combined_crisis | lp | 0.9859 | 3,103 | 145,000 | 0 | 3.8 |

Reading it: rule and engine tie on service level; the engine ships ~25-35% less fuel. LP ships less again but leaves 297-454 L unmet in three scenarios. The residual 2,979-3,103 L in the combined crisis comes from the scripted Cox's Bazar station outage (no policy can deliver to a closed station). "none" = no allocations. Constraint violations: 0 allocation failures in all policies.
Measurement method: `scripts/benchmark.py`, `/v1/metrics` service_level / unmet_demand_liters / allocation_liters / allocation_failures; decision time = wall time of the policy call per tick.
