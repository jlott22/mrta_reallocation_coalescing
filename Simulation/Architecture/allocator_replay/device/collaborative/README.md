# Collaborative-visit allocator ports

Replay modules named `replay_c_<algorithm>` are generated from the current
`known_visit_sim/algorithms` sources in the sibling collaborative simulator by
`allocator_replay.device.build`. Generated `.py` and `.mpy` files live under
`Simulation/Architecture/allocator_replay/builds/<build-id>/`.

Only portability imports are redirected. The allocator implementation remains
the same implementation exercised during trace capture.
