# Key decisions
1. Heuristic + forecast + constrained sizing over RL: explainable, testable, no training data; RL is optional per brief.
2. Engine chosen over plain threshold rule on *shipping efficiency* (mock: ~25-35% less fuel at equal service level), not service level (tied).
3. Error taxonomy from the guide: 404/409/422 permanent, 503 transient. Circuit breaker + cached state for outages.
4. Incidents split: `crisis` (world events) vs `service_fault` (dependency failures) so operators are not misled.
5. Health endpoint cached 2 s so dashboard load cannot amplify simulator load (found via load test).
6. Engine avoids routes with *scheduled* disruptions at departure (bug found in demo; regression test added).
7. Deterministic briefing text; no LLM dependency (gap: no generative-AI layer).
8. LP implemented and benchmarked but not default: it ships ~9-12% less fuel yet leaves 297-454 L unmet in 3 of 4 scenarios; MILP and graph routing not needed (routes are 6 fixed one-hop edges, no coordinates; see route-and-map-analysis.md).
9. Added read-only `/api/simulate` because the brief's loop has a Simulate step (§2, §22).
10. `scripts/evaluate.py` reports SKIPPED separately from FAIL so an unavailable dependency is not confused with an app defect.
