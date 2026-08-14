# Causal implementation report

> **Archived pre-correction implementation report.** The source has since
> adopted the strict-bound, message-only, non-destructive architecture in
> `docs/SIMULATION_ARCHITECTURE.md`. Results and validation recorded here must
> not be treated as validation of the corrected rerun.

Date: 2026-08-10 (America/Los_Angeles)

## Status and scope

The repository now contains a separate causal campaign path for the IEEE IPCCC
reallocation-coalescing study. It replaces the earlier post-hoc addition of
allocator time with an event-driven mission in which each logical robot is
blocked by its own measured allocation duration. Movement is also represented
as an interval, so position and task completion are committed only at movement
completion.

The implementation has now passed its software suites plus a physical
three-board AGX preflight and focused longitudinal parity probes. Virtual
devices and loopback tests remain non-hardware evidence. Publication launchers
still fail closed until the controlled AGX environment, smoke, calibration,
freeze, full causal, and counterfactual gates pass from a clean commit.

This work is confined to the standalone `mrta_reallocation_coalescing`
repository. The previous implementation and pilot reports remain in place as
historical evidence; they are not the protocol for the causal paper campaign.

## Scientific abstraction

The correct description of one publication mission is:

> One physical RP2040 per simulation provides measured allocator durations for
> four persistent logical robot contexts; those durations are injected into a
> virtual four-processor decentralized team model.

Suitable short names are **RP2040-timed causal virtual mission** and
**hardware-timed four-processor simulation**. Three boards run three independent
missions concurrently. The implementation must not be described as four
physical RP2040 processors executing one mission, a moving-robot hardware
experiment, or a measurement of physical mission elapsed time.

The AGX simulator remains authoritative for allocation decisions. The RP2040 is
a parity-checked duration source. Compared policies receive the same exogenous
scenario, release trace, and applicable seeds, but their decisions, call
counts, paths, task completions, and makespans may diverge. Those downstream
differences are part of the treatment effect.

## Causal event model

`AsyncTrialRunner.run_online_trial()` drives a discrete-event queue containing
absolute releases, coalescing timeouts, robot control boundaries, compute
completions, movement completions, and communication delivery. There is no
global team pause.

At a safe robot control boundary:

1. The eligible robot freezes its allocator input and starts compute at virtual
   time `t`.
2. The AGX executes the authoritative allocator call against that snapshot.
3. The bound RP2040 context executes the parity-equivalent call and reports a
   device-only duration `d_r`.
4. The robot is compute-busy until `t + d_r`; its staged goal, messages, and
   post-state are not visible before that completion.
5. Other robots continue to move, finish tasks, receive events, or compute.
6. Releases remain on their predeclared absolute time axis. Inputs arriving
   during compute are buffered for the next eligible invocation and cannot
   leak into the in-flight call.

Communication for the paper configuration is ideal but has an explicit fixed
delivery delay of 40 ms and zero communication jitter. It is not a measured
radio model.

The event engine uses predeclared guardrails of 251,000 total causal events and
5,500 events without progress. Crossing a guardrail produces a retained,
technically valid `algorithmic_incomplete` outcome (`event_horizon` or
`stagnation_horizon`); it is not mislabeled as a transport failure. Exhausting
the event queue is likewise an algorithmic outcome. Transport, parity, device,
binding, and malformed-output faults are technical failures and are eligible
only for the configured bounded technical retry.

### Same-time call groups

All logical calls that start at the same simulated timestamp are frozen before
the first authoritative or physical result can mutate visible state. Physical
requests may then be measured sequentially on the worker's single board. Their
virtual completions remain independent:

```text
common start t
R0 completes at t + d0
R1 completes at t + d1
R2 completes at t + d2
R3 completes at t + d3
```

The implementation does not use `t + d0 + d1 + ...`. Provider inputs are
detached copies, group/call identifiers are unique, and device post-state is
published only after the group has passed parity. A message from one member of
the group therefore cannot change a peer call that already began at the same
virtual instant.

## Movement and release semantics

A grid traversal is now explicit:

```text
movement_start(t) -> moving -> movement_complete(t + d_move)
                  -> position update -> arrival/task completion
```

The final required task is timestamped only after its final movement duration.
Consequently, for a completed mission:

`mission_elapsed_time_s = final required task completion time - mission start`

Allocator work is not added afterward, and host wall runtime remains a separate
diagnostic. An algorithmically incomplete mission has no final-required-task
timestamp and therefore no scientifically defined mission makespan; its event-
horizon clock is diagnostic only and must not enter makespan analysis.

The inherited numerical movement model is retained as a virtual model; no
Pololu speed is claimed. Movement jitter is deterministic and exogenous. Each
duration is keyed by the runtime seed, trace ID, logical robot ID, that robot's
action index, and directed edge. This makes a given movement action independent
of host/provider interleaving while allowing policy-induced trajectories to
diverge. The timing-model name, seed, action index, and SHA-256 key are recorded
with every movement.

Every task preserves separate release, pending, admission, first eligible
allocator start, first assignment, and completion timestamps. A release can be
admitted while one or more robot processors are busy. Validation rejects
assignment before release and rejects a movement-serviced completion whose
timestamp differs from the associated movement completion.

## Coalescing layer

The common scheduler supports:

- Eager / B=1;
- count B=2;
- count B=4;
- count B=8; and
- bounded B=4 with a common timeout W.

Arrival-driven triggers (`task_arrival_eager`, `batch_threshold`,
`age_timeout`, and `final_release_flush`) are separated from mandatory or
execution-driven triggers (`initial_allocation`, `task_completion`,
`invalid_goal`, `robot_idle`, consensus/internal activity, and explicit other
reasons). Pending tasks may be piggybacked into a mandatory reallocation and
that admission is logged. Thus B=4 means that four pending arrivals trigger an
arrival-driven reallocation; it does not promise that every solve sees exactly
four new tasks.

## RP2040 timing boundary and transport accounting

The native protocol uses timing-decomposition schema 2. The causal compute
interval uses only the on-device allocator duration.

| Field | Meaning | Enters virtual compute? |
|---|---|---:|
| `rp2040_device_duration_s` | RP2040 goal-selection plus policy-induced epoch-reset time | yes |
| `rp2040_choose_goal_duration_s` | RP2040 goal-selection component | component of prior row |
| `rp2040_algorithm_epoch_reset_duration_s` | RP2040 allocator epoch-reset callback component | component of prior row |
| `agx_allocator_duration_s` | authoritative AGX goal-selection plus epoch-reset work | no |
| `agx_choose_goal_duration_s` | AGX goal-selection component | component of prior row |
| `agx_algorithm_epoch_reset_duration_s` | AGX allocator epoch-reset callback component | component of prior row |
| `psetup_transaction_s` | host wall time for PSETUP/setup transaction | no |
| `device_pre_call_setup_s` | on-device state/event/epoch setup and explicit pre-call GC | no |
| `ptime_result_transaction_s` | PTIME/result transaction wall time | no |
| `serial_roundtrip_s` | PSETUP plus PTIME/result transaction wall time | no |
| `host_prepare_cpu_s` | separately recorded host preparation CPU work | no |
| `host_total_call_s` | complete host-side call diagnostic | no |
| `host_serialization_setup_s` | zero placeholder in schema 2 | no |

The device total is the exact sum of two separately checked regions:
`choose_goal()` and, on the first call for an admission epoch, the allocator's
policy-induced `on_allocation_epoch()` callback. It excludes generic PSETUP
state synchronization and message application, explicit pre-call GC, post-call
GC, message/state snapshot generation, serialization, and USB result transfer.
Garbage collection that occurs naturally inside either measured allocator
operation remains included. The device emits a `PTIMED` frame before output
construction. The AGX authoritative timer uses the same two-component scope.

The protocol cannot isolate pure host serialization/setup from USB and
device-side setup. Therefore `host_serialization_setup_s` is explicitly zero
and `host_serialization_setup_measured=false`; it must not be interpreted as a
measurement. `serial_roundtrip_s` is also not pure USB latency: it is defined as
the sum of PSETUP transaction wall time and PTIME/result transaction wall time.
This limitation is sealed in every hardware attestation. Timer unit,
resolution, monotonicity, and wraparound-safe `ticks_diff` behavior are checked
by preflight.

## Device context lifecycle

Each worker holds one exclusive board lease and one serial session. A mission
creates exactly four resident logical contexts, one per simulated robot. The
contexts retain allocator state across calls in that mission and interact only
through explicitly delivered simulated events/messages.

Before the next mission, the host soft-resets into raw REPL and starts a clean,
motor-free replay worker. This bypasses `main.py`, clears the MicroPython heap
and module state, revalidates the live identity/build/firmware fingerprint, and
then deterministically initializes four contexts from the sealed algorithm,
scenario, condition, and runtime seed. Ending a mission clears the trial and
host-side context/session bookkeeping. This reset/setup work is outside the
allocator timer.

Explicit pre-call GC is performed outside the allocator component timers and is
measured as device setup; post-call cleanup/serialization GC is also outside.
Natural GC during goal selection or the allocator epoch-reset callback remains
part of measured allocator cost.

## Authoritative decisions and parity

The AGX result governs virtual mission behavior. A duration may enter the event
queue only after the native call matches the frozen logical computation.
Hardware calls carry a sealed per-call attestation containing board label and
device UID, serial endpoint, build ID, firmware and module-set hashes, trial,
condition, group/call/attempt/context IDs, timer evidence, timing decomposition,
call class, candidate count, goals, and parity hashes.

Parity is strict when canonical representations are identical. Where desktop
and compact native layouts differ, algorithm-specific behavioral projections
cover state that can affect later behavior: task universe and active set,
probabilities, position/peers, path/bundle, claims and protocol timestamps,
collision memory, protocol counters, epoch metadata, pending and last-sent
communication state, and HIPC prediction/drop state. Ordered outbound message
content, goal, active/candidate counts, and call classification are also
checked. Missing non-default projected behavior is a mismatch.

Any goal, message, post-state, call-class, count, identity, timer, or attestation
mismatch invalidates the call/session. Its duration is not accepted, guessed,
or silently resynchronized. Disconnects invalidate an in-flight mission;
reconnection is allowed only between missions and only after exact
identity/build revalidation. CBAA, ACBBA, PI, and HIPC are the blocking primary
algorithms. Optional DMCHBA/DGA support is outside the full-campaign gate.

## Three-worker native orchestration

Publication stages require exactly three processes and three unique bindings:

```text
worker 0 / one mission at a time -> RP2040 A -> four logical contexts
worker 1 / one mission at a time -> RP2040 B -> four logical contexts
worker 2 / one mission at a time -> RP2040 C -> four logical contexts
```

Stable device UID/build/firmware/module identities are used rather than
`/dev/ttyACM*` enumeration order. Global UID leases prevent two processes or
campaigns from opening one board. On native Linux each worker is pinned to its
configured distinct core. OMP, OpenBLAS, MKL, NumExpr, vecLib, and BLIS thread
counts are forced to one. The environment gate also verifies that three workers
do not exceed 75% of available logical cores; the campaign worker count itself
is fixed at three, not computed from that percentage.

The paired block is `(algorithm, load, trace)`. All five policies remain on one
physical board. Blocks use a crossed Latin board assignment, deterministic
hash-based execution order, and per-board cyclic policy rotations. For the
target 4 x 3 x 25 design, each board receives 100 blocks and every policy occurs
in every order position 20 times on each board. At n=50 those values become 200
blocks and 40 occurrences per policy position on each board.

## Calibration, freeze, and campaign gates

Calibration uses master seed `20260808`. The final cohort is independently
generated from master seed `2026080905`; freeze validation rejects calibration/
final trace overlap. The pipeline is:

1. read-only AGX environment report;
2. twenty-check three-board native parity preflight;
3. 32-mission causal smoke, including a second content-validated resume pass;
4. 280-mission native rate calibration;
5. 300-mission bounded-timeout calibration;
6. 240-mission variance/n check;
7. explicit operator-reviewed design freeze;
8. target 1,500-mission native causal campaign;
9. matching 1,500-condition zero-compute campaign; and
10. trial-level analysis and final report.

The design freeze seals source/commit identity, effective config and manifest
hashes, final rates, W, n, algorithms, policies, final release manifests,
three-board UID/build/firmware/module cohort, gate hashes, schedule inputs, and
analysis version. A source, configuration, manifest, board, or build change
after freeze fails closed rather than silently creating a mixed campaign.

Attempts and promoted outputs are non-overwriting. Promotion requires typed and
semantic validation, then records required-output hashes in `completion.json`.
Resume rehashes and revalidates completed jobs. Technical attempts are retained
with diagnostics and a bounded retry history. Algorithmically incomplete or
scientifically unfavorable outcomes are retained and reported, not retried as
infrastructure faults.

## Metrics and raw evidence

For a mission with allocator calls `c`:

```text
W_alloc_rp2040 = sum_c device_allocator_duration_s(c)
W_alloc_agx    = sum_c agx_allocator_duration_s(c)
capacity       = W_alloc_rp2040 / (4 * mission_elapsed_time_s)
```

Both W values are processor-seconds, not delay. For a policy P paired to Eager
within the same algorithm/load/trace block:

```text
percent work saved(P) = 100 * (W_eager - W_P) / W_eager
```

Task-level release-to-first-assignment and release-to-completion latencies are
derived from the explicit lifecycle timestamps. The independent statistical
replicate is the trial, so per-task rows are descriptive within-trial evidence.

For exact causal/zero pairs:

```text
D_alloc = T_mission_causal - T_mission_zero
allocation-attributable fraction = D_alloc / T_mission_causal
```

Negative values are flagged and retained rather than clipped. Because zero
duration can change event ordering, calls and trajectories need not match.

Every promoted job contains:

- `trial_summary.json`;
- `task_events.csv`;
- `allocator_calls.csv`;
- `reallocation_events.csv`;
- `movement_events.csv`;
- `run_provenance.json`; and
- promotion metadata and hashes in `completion.json`.

The validator recomputes task lifecycle arithmetic, call completion `t+d`,
group associations, mechanism/event counts, work totals, movement durations,
steps, final completion/makespan, board/build identity, parity attestations, and
provenance bindings before promotion or resume.

## Analysis products

The causal analysis uses trials as replicates, forms only complete paired policy
blocks, runs Friedman tests within each algorithm/load, and conditionally runs
paired Wilcoxon signed-rank comparisons against Eager with Holm correction.
Deterministic paired bootstrap intervals support effect magnitudes. It also
retains an outcome/exclusion audit, exact zero-pair coverage, B4 workload
dependence, and the inputs for the three intended paper figures. No outcome or
significance direction is hard-coded.

## Defects addressed by the rewrite

- Allocator duration is no longer added after a trajectory has already run.
- One board's sequential sampling order cannot serialize virtual robot compute.
- Same-time calls cannot observe another member's newly computed state.
- Releases during compute remain absolute and cannot leak into the in-flight
  snapshot.
- Movement and final-task service are committed at arrival, not departure.
- Communication jitter cannot create a policy-specific random stream; the paper
  configuration uses fixed 40 ms delay and zero jitter.
- Movement randomness is keyed to exogenous action identity rather than event
  interleaving.
- Hardware validity cannot be asserted from a board name or virtual provider;
  exact sealed attestation is required.
- Technical failures and algorithmic noncompletion have separate status paths.
- Calibration and final cohorts use different master seeds.
- Full-campaign board assignment balances algorithms, loads, traces, and policy
  order while preserving within-block board pairing.

## Validation status

Current local validation passes 185/185 automated tests: 57 simulator/core, 48
study/campaign/reporting, and 80 device/HIL protocol tests. Python compilation,
all AGX shell-script syntax checks, causal CLI entry points, and patch hygiene
also pass. Development providers remain explicitly non-hardware-valid.

The integration process found and fixed two cross-layer defects before the
final v5 run: device timing-component schema drift and loss of precision when a
64-bit runtime seed was validated through a floating-point conversion. Native
longitudinal tests also exposed and fixed stale message suppression and an
epoch-reset timing bias: the RP2040 now replays the epoch event against the
resident populated pre-hook allocator state, times the algorithm-specific
reset and protocol-cache clearing, then restores the authoritative AGX
post-hook checkpoint for goal-selection parity. Failed versioned development
attempts were retained instead of overwritten. Exact commands, hashes, and row
counts are in `IMPLEMENTATION_TEST_REPORT.md`.

Physical evidence obtained on the AGX:

- three stable Pololu UIDs bound to three fixed workers/cores;
- sealed MicroPython 1.24 build
  `micropython_1_24_o0_coalescing_collaborative_178fdb38cac5` deployed without
  changing `main.py` or initializing motors/sensors;
- all twenty native preflight checks PASS on all three boards, including 72
  parity-valid calls and 51--54 microsecond timer-resolution evidence; and
- the exact prior PI and ACBBA failure trajectories pass through call 115 on
  hardware (115 parity-valid calls each).

Publication work still required:

- record and deliberately accept a stable AGX power/clock/thermal state;
- complete smoke, calibration, reviewed freeze, full causal, and zero-compute
  stages without identity drift; and
- review the generated reports before using any value in the paper.

Until those checks pass, the implementation is **software validated and
physical-preflight validated, but not publication-campaign validated**. See
`AGX_NATIVE_RUNBOOK.md` for the only publication execution sequence and
`NATIVE_VALIDATION_CHECKLIST.md` for the operator record.
