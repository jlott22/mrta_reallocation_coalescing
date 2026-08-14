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
    "max_candidate_cells": None,
})
runtime.reset_trial({}, {
    "pos": [0, 0],
    "active_tasks": [[2, 2], [8, 4]],
    "peer_positions": {"01": [0, 6], "02": [0, 12], "03": [0, 18]},
})
runtime.apply_delta({"sequence": 1, "pos": [1, 0]})
runtime.apply_delta({
    "sequence": 2,
    "events": [{
        "kind": "allocation_epoch",
        "payload": {
            "epoch_index": 1,
            "trigger_reason": "batch_threshold",
            "admitted_cells": [[7, 3]],
        },
    }],
})
decision = runtime.choose_goal()
messages = runtime.drain_messages()
```

`reset_trial(config, initial_state)` creates one persistent robot/allocator
instance. `apply_delta(delta)` changes only movement, target completion,
probability, collision, peer-position, and peer-message state. Duplicate
sequence numbers are ignored. It also accepts the worker's standard
`{"set": sectioned_state, "delete": ..., "events": ...}` delta.
Future task coordinates must not be included in `all_tasks`, `task_universe`,
or an authoritative full active-set replacement. A delivered
`allocation_epoch` event appends any newly known coordinates to the resident
registry before they become allocator candidates.

`choose_goal()` returns a small object with `.goal` and `.debug`; the shared
worker puts the outer timer immediately around this complete allocator
transaction. It first applies queued admission hooks, peer messages, completion
hooks, and allocator-local recovery, then runs ordinary goal selection. The
runtime's
`timing_counters()` exposes nested candidate-filter samples and
`candidate_counts()` exposes the before/after counts, allowing the worker to
report total, filter, and allocator-exclusive microseconds. USB decoding,
delta staging, outbound-message draining, and snapshots stay outside that timer.

The coalescing host sends one `allocation_epoch` event to each robot context
after that robot receives an admission announcement. The persisted event record
includes the epoch index, trigger reason, and admitted cells. Duplicate delivery
is idempotent. Admission is non-destructive: the allocator may invalidate an
active-set-dependent probability cache, but does not clear a valid goal, claim,
bundle, or path. The candidate set remains the complete locally known active set
(`max_candidate_cells=None`). CBAA remains single-assignment; ACBBA, PI, and
HIPC have no bundle-size cap.

For CBAA, a retained claim keeps its original auction-time bid across movement.
Position deltas may validate the claim but must not refresh its value or emit a
new bid. This matches the desktop allocator and prevents delayed old/new bid
oscillation for the same owner.

`snapshot_minimal()` returns the five standard worker sections. Its one compact
resume record contains target flags, claims, allocator paths, RNG state, and the
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

The current experimental algorithm names are `CBAA`, `ACBBA`, `PI`, and
`HIPC`. `DMCHBA` and `DGA` remain in the runtime only for legacy fixtures and
must not enter corrected campaign manifests. Collaborative targets normally all
have probability 1, so the shared normalized probability cost reduces to route
distance while retaining the same scoring definition when a nonuniform fixture
is supplied.
