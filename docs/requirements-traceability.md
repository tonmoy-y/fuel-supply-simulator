# Requirements traceability
Sources: brief = scanned participant brief (PDF page → printed page in brackets); guide = Final Integration Guide (§). **All "Verified" below means verified against the MOCK simulator.**

**Unresolved:** the brief scan skips printed page 8 (PDF p.7 = printed 7, PDF p.8 = printed 9). §15-§17 and the start of §18 (security) are not readable, so requirements there (likely testing/performance/security) are untraced. Needed: a scan of printed page 8.

| ID | Requirement (brief) | Level | Criterion (weight) | Implementation | Verification / evidence | Status |
|---|---|---|---|---|---|---|
| R1 | Operator app, not a notebook; subset of §6 items (inventory, status, demand, alerts, risk, incoming supply, disruptions, recommendations, impact, alerts, history, health) [p.4 §6] | Mandatory | Product/UX 20 | `static/index.html`, `/api/*` | API tests; JS syntax check; **not opened in a browser** | Built, UI unverified |
| R2 | ≥1 meaningful AI/ML/optimization/detection capability [p.2 glance, p.5 §7] | Mandatory | Intelligence 20 | `forecast.py`, `engine.py`, `optimizer.py` | 71 tests, benchmark | Verified (mock) |
| R3 | Inspectable recommendations: signals, constraints, impact, uncertainty, alternatives [p.6 §9] | Recommended | Intelligence 20 | recommendation `explanation` payload | `test_demand_spike_produces_valid_explainable_recommendations` | Verified (mock) |
| R4 | Loop incl. **Simulate** [p.3 §2; p.10 §26] | Mandatory (demo) | Demo 10 / Product 20 | `POST /api/simulate`, dashboard "Simulate" button | 3 tests | Verified (mock); button UI unverified |
| R5 | Crisis handling: shipment delay, demand spike, depot constraint, regional disruption, combined [p.6 §10] | Mandatory (demo) | Resilience 10 | engine schedule-aware routing, incidents | benchmark 4 scenarios, `demo-run.json` | Verified (mock) |
| R6 | Resilience matrix: ML unavailable→fallback; invalid response→reject+alert; low confidence→review; dependency down→retry/cache/degraded [p.6-7 §11] | Mandatory | Resilience 10 | `fallback_policy`, client validation, `requires_review`, breaker + cache | `test_simclient.py`, demo steps | Verified (mock) |
| R7 | Reproducible deploy, preferably containerized; CI encouraged [p.7 §12] | Mandatory | DevOps 15 | `Dockerfile`, `docker-compose.yml`, `.github/workflows/ci.yml` | files present (S1) | **Not executed** |
| R8 | Observability: app, system, intelligence, logs layers [p.7 §14] | Mandatory | Observability 10 | `/metrics` (http, process cpu/mem, MAPE, confidence share, alerts, fallback, decisions), JSON logs | live `/metrics` listing | Verified (mock) |
| R9 | Load-test evidence with workload definition [p.8 §19.10] | Mandatory | Observability 10 | `scripts/loadtest.py` | `evidence/loadtest.json`: 25 users/15 s, 331 rps, p95 63 ms, 0 err | Verified (app APIs, mock) |
| R10 | Architecture diagram [p.8 §19.6] | Mandatory | Architecture 15 | `docs/architecture.md` (text diagram) | review | Built |
| R11 | Resilience demonstration [p.8 §19.9] | Mandatory | Resilience 10 | `scripts/demo.py` | `evidence/demo-run.json` (13 steps) | Verified (mock) |
| R12 | Final demo following §22 story [p.9 §22] | Mandatory | Demo 10 | `scripts/demo.py` (14 steps incl. what-if) | `evidence/demo-run.json`; needs re-run vs real sim | Verified (mock) |
| R13 | Guardrails: simulated only, document assumptions, human review [p.10 §24] | Mandatory | all | no reset code path (S3), admin proxy off, `requires_review` | evaluate.py S3 | Verified |
| R14 | Security: document config, no credentials, restrict operator actions [p.8 §18 tail] | Recommended | DevOps 15 | `.env.example`, `OPERATOR_TOKEN` | S4 secrets scan | Verified (basic) |
| R15 | Generative AI (incident explanation etc.) [p.5 §7] | Optional | Intelligence 20 | deterministic briefing only | - | **Not built** |
| R16 | Reinforcement learning [p.5 §8] | Optional | - | - | - | Not built |
| R17 | Kubernetes/Helm/etc. [p.7 §13] | Optional | - | - | - | Not built |
| R18 | Simulator contract (single write, idempotency key, 404/409/422 permanent, 503 transient, SSE advisory) [guide §2, §5, §9] | Mandatory | Architecture 15 | `simclient.py`, `engine.validate_allocation` | contract tests vs mock | Verified (mock) only |

## Rubric coverage (what judges see / what could cost points)
| Criterion | Judges expect | Proof | Deduction risks | Show live | Unverified |
|---|---|---|---|---|---|
| Product/UX 20 | working workflow | dashboard + API | UI never opened in a browser | approve/simulate/dismiss flow | rendering, mobile |
| Intelligence 20 | useful, appropriate method | forecast MAPE metric, engine vs rule vs LP table | engine ties rule on service level (advantage = less fuel); mock-only | spike → recommendation → explanation | real-sim tuning |
| Architecture 15 | clean integration | adapter, validator, docs | never run on real simulator | architecture.md | real contract differences |
| DevOps 15 | deploy + CI + tests | compose, CI file, 71 tests, evaluate.py | compose/CI never executed | `docker compose up` | everything containerized |
| Resilience 10 | failure handling | demo steps, breaker, fallback | only injected mock faults | fault → degraded → recovery | real fault behavior |
| Observability 10 | metrics/logs/load | /metrics, JSON logs, loadtest | load test is on cached app APIs, not sim | /metrics, service health panel | dashboards (no Grafana) |
| Demo 10 | story, constraints | demo.py, docs | 13 vs 14 story steps | scripted run | rehearsal on real sim |
