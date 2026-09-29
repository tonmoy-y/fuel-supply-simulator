# Route, graph and map analysis
Question: must routes be discovered from a map or via shortest-path search? **No, per the sources reviewed.**

| Question | Evidence | Finding |
|---|---|---|
| Are routes predefined? | Guide §4.7 `GET /v1/routes`; §8.4 lists 6 routes with `source_depot_id`, `destination_station_id`, `transit_ticks`, `max_shipment`, `status` | Yes: 6 fixed depot→station edges (transit 2-4 ticks, max 5,000-7,000 L). |
| Is multi-hop routing possible? | Guide §5.2: an allocation names one `route_id` whose endpoints must equal (depot, station) else `ROUTE_MISMATCH` (409) | No. The graph is bipartite, single-hop; "route choice" = choosing among ≤2 direct edges per station. Dijkstra/A* would have nothing to search. |
| Are coordinates / geometry supplied? | No coordinate, polyline or distance fields in the guide's route/station schemas (searched: latitude, coordinate, polyline, google, map) | No. A map service would add a dependency with no data to bind it to. |
| Do disruptions matter? | Guide §5.2 `ROUTE_DISRUPTED`; event `route_disruption` (§7.8); FAILED allocations if disrupted at departure (`allocation_failures`) | Yes, and the engine already handles it, including *scheduled* disruptions covering the departure tick (bug found in demo; regression test). |
| Does the brief require maps? | Brief §3 (supply chain), §4 (conceptual endpoints `GET /routes`), §7, §10 | No mention of map routing. |

Decision: use the simulator's route table as authoritative; no external map service; no shortest-path algorithm. Revisit only if the organizers introduce multi-hop routes or geometry. Limitation noted in `optimization-design.md`: the guide (§9 status table, ROUTE_CAPACITY_EXCEEDED: "Split into multiple smaller allocations") allows several allocations per route per tick; the LP models one shipment per (route, fuel) per tick, so it is slightly conservative.
