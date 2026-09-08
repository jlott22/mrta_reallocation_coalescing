# Architecture and changes

The corrected experiment uses one architecture rather than a compatibility
mode.

- Eager admits exactly one task; Count admits exact B-sized batches; Bounded
  admits at B or W. Completion/idle/invalid calls never drain pending work.
- The sole sub-B Count tail is a terminal residual after every previously
  admitted task is physically complete.
- Robots learn tasks, completions, state, and allocator information only from
  messages. Initial tasks use the same announcement path.
- Admission is non-destructive: it preserves the executing goal, motion,
  claims, logical clocks, paths, and bundles.
- CBAA sees the full admitted pool and owns one task. ACBBA, PI, and HIPC have
  uncapped bundles/paths.
- A retained CBAA claim keeps its auction-time bid across movement. Position
  changes cannot produce a new bid or rebroadcast; a real outbid, completion,
  invalidation, recovery release, or new selection is required.
- ACBBA/HIPC retain their suffix-release rules. PI removes completed/lost items
  individually. These are algorithm-native differences, not simulator policy.
- Idle robots sleep until a message or local deadline. Recovery is targeted,
  robot-owned, and never a centralized reset.
- The allocator timer covers queued allocator inputs, admission/completion
  hooks, local recovery, and goal selection. Transport, decoding, serialization,
  and outbound extraction remain outside.
- RP2040 contexts register admitted tasks dynamically and use the same
  non-destructive state and timing boundaries as the desktop allocator. Four
  logical robot checkpoints are time-multiplexed through one resident native
  runtime per board call; reconstruction is complete and occurs outside
  `W_alloc`, with no task/path horizon.

Canonical detail is in `docs/SIMULATION_ARCHITECTURE.md` and the corrected
pilot record is in [`PILOT_REPORT.md`](PILOT_REPORT.md).
