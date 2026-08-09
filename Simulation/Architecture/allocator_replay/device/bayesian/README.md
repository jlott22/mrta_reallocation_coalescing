# Bayesian allocator ports

Replay modules named `replay_b_<algorithm>` are generated from the current
`Simulation/Architecture/simulator/benchmark_sim/algorithms` sources by
`allocator_replay.device.build`. Generated `.py` and `.mpy` files live under
`Simulation/Architecture/allocator_replay/builds/<build-id>/` so authoritative simulator
logic is never copied here and allowed to drift.

The generator redirects only portability dependencies (types, timing, random
state, compact maps, copying, and hashing). It does not import or modify any
`Hardware/Algorithms/Pololu_*.py` program.
