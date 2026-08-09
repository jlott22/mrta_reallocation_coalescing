# Native collaborative-visit allocators

This package is a motor-free, MicroPython-compatible allocator core for the
50-known-target collaborative-visit mission. It does not import either desktop
simulator and it does not initialize motors, sensors, UART, USB, or radio.

The hardware-in-the-loop adapter and a future physical wrapper use the same
facade:

```python
from allocator_replay.device.native.collaborative import create_persistent_runtime

runtime = create_persistent_runtime({
    "algorithm": "CBAA",
    "robot_id": "00",
    "robot_ids": ["00", "01", "02", "03"],
    "grid_size": 19,
    "max_candidate_cells": 25,
})
runtime.reset_trial({}, {
    "pos": [0, 0],
    "active_tasks": [[2, 2], [8, 4]],
    "peer_positions": {"01": [0, 6], "02": [0, 12], "03": [0, 18]},
})
runtime.apply_delta({"sequence": 1, "pos": [1, 0]})
decision = runtime.choose_goal()
messages = runtime.drain_messages()
```

`reset_trial(config, initial_state)` creates one persistent robot/allocator
instance. `apply_delta(delta)` changes only movement, target completion,
probability, collision, peer-position, and peer-message state. Duplicate
sequence numbers are ignored. It also accepts the worker's standard
`{"set": sectioned_state, "delete": ..., "events": ...}` delta.
`choose_goal()` returns a small object with `.goal` and `.debug`; the shared
worker puts the outer timer immediately around this method. The runtime's
`timing_counters()` exposes nested candidate-filter samples and
`candidate_counts()` exposes the before/after counts, allowing the worker to
report total, filter, and allocator-exclusive microseconds. USB decoding,
delta application, message draining, and snapshots stay outside that timer.

`snapshot_minimal()` returns the five standard worker sections. Its one compact
resume record contains target flags, claims, short paths, RNG state, and the
DGA population where applicable. This lets a controller switch simulated
robot contexts outside the timed region without changing the allocation state
that a continuously running physical robot would retain.

For DGA, each saved population plan is a separate result field. The worker
therefore serializes and transfers one small plan at a time instead of
allocating a large JSON document containing all 30 plans.

Internally, cells are unsigned 16-bit numbers and the 50 target flags,
probabilities, owners, claim values, and claim epochs are parallel arrays.
DMCHBA evaluates Hungarian costs on demand instead of allocating a Python
matrix. DGA deliberately retains the study configuration of 30 plans and 25
generations and implements random-segment crossover plus move, cross-route
swap, reinsert, partial reverse, and cleanup mutations. Slice reversal uses a
concrete list, which is accepted by MicroPython.

This DGA is materially different from the older Bayesian Pololu program, not
just a different spelling of the same operation. The older program searched
12 plans for 8 generations, crossed fixed halves, and had only move,
same-route swap, and whole-route reversal. This package searches 30 plans for
25 generations. Random-segment crossover can inherit useful subsequences from
any part of either parent, while the five mutation families can also transfer
work between robots and change only part of a route. The larger search can
therefore return a different plan and intentionally does substantially more
allocator work.

The six algorithm names are `CBAA`, `ACBBA`, `PI`, `HIPC`, `DMCHBA`, and
`DGA`. Collaborative targets normally all have probability 1, so the shared
normalized probability cost reduces to route distance while retaining the same
scoring definition when a nonuniform fixture is supplied.
