# Corrected native Bayesian allocator layer

This package is the shared, motor-free allocator layer for stationary HIL and
future physical Pololu wrappers. It does not import or modify any
`Hardware/Algorithms/Pololu_*.py` program.

The shared objective is:

```text
cost = distance + 8 * (1 - target_p[cell] / max(target_p))
score = -cost
```

The maximum probability is cached until a belief delta invalidates it. CBAA,
ACBBA, PI, HIPC, DMCHBA, and DGA all consume this same scorer.

DMCHBA uses a virtual Hungarian matrix with reusable linear work arrays. DGA
uses the study's full population of 30, 25 generations, random-segment
crossover, and move/swap/reinsert/segment-reverse/clean mutations. DGA target
cells are stored as 16-bit integers instead of `(x, y)` tuples.

`create_persistent_runtime(config)` exposes:

- `reset_trial(config, initial_state)`
- `apply_delta(delta)`
- `choose_goal()`
- `drain_messages()`
- `snapshot_minimal()`

CBAA, ACBBA, PI, and HIPC require their complete consensus/message adapters in
campaign use. The facade raises instead of silently treating their local
scoring core as the full algorithm. The opt-in
`allow_diagnostic_local_core=True` setting exists only for focused scorer
diagnostics. The complete generated MicroPython ports supply those consensus
adapters in HIL; future physical programs reuse the same adapter and core.
