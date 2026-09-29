# Optimization design
**Requirement status:** the brief requires "at least one meaningful AI/ML/optimization/detection capability" (§7, p.5) and does not mandate a technique. Two allocators are implemented and compared; the greedy/risk engine is the default.

## Candidates
| Approach | Where | Verdict |
|---|---|---|
| Threshold rule | `engine.fallback_policy` | Baseline + ML-unavailable fallback (brief §11). |
| Risk-projection engine (greedy, bisection sizing, shared budgets) | `engine.recommend` | **Default.** Explainable, best measured service level. |
| **LP** (continuous) | `optimizer.lp_recommend` (scipy HiGHS) | Implemented, benchmarked, available; not default. |
| MILP | - | Not needed: quantities are continuous litres; 100 L / 500 L minimum are applied by rounding then re-validated. Would be justified only for indivisible/binary route-activation decisions, which the guide does not have. |
| Graph search | - | Not applicable (see route analysis). |

## LP formulation (per decision cycle)
Sets: R = feasible (route r, fuel f) pairs (route AVAILABLE, depot OPEN/CONSTRAINED, station OPEN, no known disruption at departure tick+1); (s,f) station/fuel pairs; k = 1..H ticks (H = 24, 6 h).
Variables: `x[r,f] ∈ [0, max_shipment_r]` litres shipped; `u[s,f,k] ≥ 0` projected shortfall.
Objective: min Σ W_k·u[s,f,k] + ε·Σ x, with W_k = 1 + 0.5(H−k)/H (earlier shortfalls weigh more), ε = 0.001 (avoid pointless shipping).
Constraints:
1. Coverage: `base[s,f,k] + Σ_{r→s, 1+transit_r ≤ k} x[r,f] + u[s,f,k] ≥ z·σ[s,f,k]`, z = 1.645. `base` = inventory + already-committed inbound − cumulative forecast demand (from `engine.project`).
2. Depot inventory: `Σ_{r from d} x[r,f] ≤ inventory[d,f] − 5%·capacity[d,f]` (reserve).
3. Depot dispatch: `Σ_{r from d, all f} x ≤ dispatch_capacity_per_tick − already-dispatched`.
4. Station headroom: `Σ_{r→s} x[r,f] ≤ capacity − inventory − inbound`.
Infeasibility: constraints are all ≤ with slack `u`, so the LP is always feasible; "no good answer" shows up as large `u`. Solver failure / non-optimal status / time limit (2 s) → returns `[]`, records status in `stats`, caller falls back to the engine.
Safety: every LP result is rounded down to 100 L, dropped if < 500 L, then **independently validated** by `engine.validate_allocation` (mirrors guide §5.2 order) with shared budgets. Tests assert the validator never has to reject an LP output (`rejected_by_validator == 0`), so the LP's own constraints are what keep it feasible (verified by mutation: removing the route filter, the dispatch row, or the inventory row each fails a test).

## Measured comparison (MOCK world, seed 12345, 192 ticks; see `evaluation-plan.md`)
LP ships ~9-12% less fuel than the engine but leaves a small unmet volume in three scenarios (service level 0.9977-0.9986 vs 1.0000; combined crisis 0.9859 vs 0.9865). It is not dominant, so it is **not** the default. Untuned: z, horizon, reserve and weights were not tuned to the benchmark (that would overfit the mock).

## Honest limits
Deterministic inputs (means + z·σ), not stochastic programming; mock demand noise/supply approximate the real simulator; 5% reserve and W_k are engineering choices, not official constraints.
