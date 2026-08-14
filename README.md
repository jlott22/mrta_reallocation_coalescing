# MRTA Reallocation Coalescing

## Current rerun architecture

New experiments use one architecture, not a selectable compatibility mode.
The simulator enforces only the release/admission boundary: Eager admits one
task, Count admits exact `B`-task batches, and Bounded admits at `B` or its
configured age timeout. Completion, invalid-goal, and idle events never
piggyback pending tasks. A final sub-`B` residual is admitted only after the
last release and after every previously admitted task is physically complete.

Robots learn tasks only through timestamped admission messages and learn peer
state/completion only through messages. New admissions do not recall a current
goal or reset allocator state. ACBBA, PI, and HIPC use uncapped bundles over all
locally known admitted tasks; CBAA stays a single-current-task auction but bids
over that same full pool. Idle robots do not poll. A robot with known unfinished
work and no goal may invoke its allocator's targeted, non-destructive recovery
only at its own liveness deadline.

One allocator timing sample includes queued allocator input handling,
allocator-native completion/admission hooks, targeted recovery, and goal
selection. Communication transport/decoding, setup, snapshots, outbound-message
construction, and serialization remain outside the timer. See
[`docs/SIMULATION_ARCHITECTURE.md`](docs/SIMULATION_ARCHITECTURE.md) for the
complete contract.

The replacement pilot selected **0.075, 0.30, and 0.60 tasks/s** and the five
policies **Eager, Count B2/B4/B8, and Bounded B4/W10**. See
[`PILOT_REPORT.md`](PILOT_REPORT.md). These values supersede every calibrated
value in the archived sections below.

The authoritative launch package for the corrected rerun is
[`AGX_CORRECTED_EXPERIMENT_HANDOFF/`](AGX_CORRECTED_EXPERIMENT_HANDOFF/README.md).
It fixes the matrix, CPU ownership, two-round sequence, brief preflight, analysis
hierarchy, and replacement-data procedure for a fresh AGX clone.

CBAA now treats its winning value as an auction-time claim: movement toward a
retained task does not recompute or rebroadcast the bid. It bids again only
after the retained claim is genuinely lost or released, preventing delayed
old/new bid messages from creating a movement-driven consensus loop. The same
rule is implemented in the desktop and native runtimes.

## Archived pre-correction results

The completed n=50 primary evaluation, hardware timing validation, verification
matrix, figures, and compact checksummed exports are available in
[`publication/aug14_final_v1/`](publication/aug14_final_v1/RESULTS.md). The
bundle is retained for provenance only: its admission and allocator-lifecycle
semantics predate the current design and its results are not valid inputs to the
rerun.

> **Historical execution path.** The calibrated noncausal
> simulation/HIL workflow described later in this file is retained as historical
> evidence. The listed causal campaign also predates the strict-bound correction
> and must not be resumed into a new dataset. It was described by
> `CAUSAL_IMPLEMENTATION_REPORT.md`, `EXPERIMENTAL_PLAN.md`, and
> `AGX_NATIVE_RUNBOOK.md`. That design used exactly three AGX workers and
> three RP2040 timing boards, feeds device allocator durations (`choose_goal()`
> plus any policy-induced allocation-epoch reset callback) into a causal
> four-logical-processor mission. Do not launch or resume either old campaign
> path for the corrected experiment; create fresh manifests and output roots
> after validation and a new design freeze.

The archived native causal entry sequence was:

```bash
bash scripts/agx_prepare_rp2040_boards.sh PORT_A,PORT_B,PORT_C
bash scripts/agx_native_environment_check.sh
bash scripts/agx_rp2040_preflight.sh
bash scripts/agx_causal_smoke.sh
bash scripts/agx_causal_calibrate_rates.sh
bash scripts/agx_causal_calibrate_timeout.sh
bash scripts/agx_causal_variance_pilot.sh
bash scripts/agx_freeze_experimental_design.sh
bash scripts/agx_run_full_causal_campaign.sh
bash scripts/agx_run_zero_compute_counterfactuals.sh
bash scripts/agx_analyze_full_causal_campaign.sh
```

These commands are preserved only to interpret the archived campaign. They are
not a launch recipe for the corrected rerun.

## Historical first-generation overview

This standalone repository implements the simulation and RP2040/Pololu HIL
study for allocator-independent coalescing of online Collaborative Visit task
arrivals. It is an isolated copy: it does not import from, modify, or require
any source repository from which components were selected.

The study compares Eager/B=1, Count B=2/4/8, and bounded B=4/W=5 s above CBAA,
ACBBA, PI, and HIPC on paired 19x19, four-robot, 50-task traces. DMCHBA and DGA
remain operational and have full-size smoke coverage. Candidate enumeration is
unrestricted; this is neither a Top-K study nor task/route bundling.

## Archived calibrated design

- Eight tasks are visible at time zero; 42 arrive online.
- Low/medium/high arrival rates are 0.075, 0.30, and 1.20 tasks per simulated
  mission-second.
- The final primary matrix is 4 allocators x 3 loads x 5 policies x 50 paired
  traces = 3,000 jobs per timing treatment.
- Every condition for one trace/load reuses byte-hashed scenario and release
  manifests plus the same runtime and Python hash seed.
- Process concurrency is always capped at `floor(0.75 * logical_cores)`; on the
  22-core development machine the cap is 16.

Calibration evidence is under `artifacts/pilots/`. The full technical record,
including timing semantics and assumptions, is in `IMPLEMENTATION_REPORT.md`.

## AGX simulation

Python 3.10 or newer is required; the active simulation/campaign path uses only
the standard library. Run from a clean committed checkout on the AGX Orin:

```bash
bash scripts/run_agx_pilot.sh
bash scripts/run_agx_full_campaign.sh
bash scripts/analyze_agx_campaign.sh configs/agx_full.json
```

Safe setup checks:

```bash
bash scripts/run_agx_pilot.sh --prepare-only
bash scripts/run_agx_pilot.sh --dry-run --job-limit 4
```

Campaigns are configuration-driven, resumable, content-validated, and
non-overwriting. A full run refuses a dirty source tree. Output goes under
`study/output/`, including per-job JSON/CSV files, immutable provenance,
failures, trial-level paired deltas, condition summaries, and three clean
paper-figure CSVs. See `study/README.md` for schemas and recovery behavior.

## RP2040/Pololu replay

The RP2040 path is motor-free and keeps persistent native contexts. In a causal
mission it receives only task coordinates already delivered to the logical
robot, plus ordered allocator messages, completion hooks, and recovery requests.
The device timer covers processing those allocator inputs and `choose_goal` as
one transaction. Host/serial setup, transport, and result extraction are
reported separately. Without connected boards, loopback/native tests establish
software semantics and protocol parity but do not provide hardware timing.

On the Windows HIL host:

```powershell
$env:PYTHONPATH=(Resolve-Path "Simulation/Architecture").Path
python -m pip install -r Simulation/Architecture/allocator_replay/requirements-host.txt
scripts/run_hil_pilot.ps1 -DryRun
```

For hardware, provide the intended robot ports explicitly during first setup;
for example:

```powershell
scripts/run_hil_pilot.ps1 -Ports COM12,COM13,COM14,COM15 -PrepareDevices
scripts/run_hil_campaign.ps1 -Ports COM12,COM13,COM14,COM15 -RunPreflight
```

The launchers validate manifest bytes before opening devices, bind trials to a
specific verified build/module/firmware identity, preserve append-only journals,
and resume completed work. No hardware was connected during development;
software loopback validation must not be cited as a physical result. Detailed
setup and output fields are in `Simulation/Architecture/allocator_replay/README.md`.

## Verification

```bash
python -m unittest discover -s known_visit_sim/tests -v
python -m unittest discover -s study/tests -v
python -m unittest discover -s Tests/HIL/AllocatorReplay -v
```

Generated manifests and raw campaign/HIL results are intentionally separate.
Do not append corrected runs to the archived manifests or output roots. Never
infer statistical significance from task-level rows; the paired trial is the
experimental replicate.

## Repository map

```text
known_visit_sim/                         Collaborative Visit simulator
known_visit_sim/core/reallocation.py    online lifecycle and scheduler
study/                                  manifests, campaigns, validation, analysis
configs/                                calibrated AGX/HIL and retained pilot configs
Simulation/Architecture/allocator_replay/  persistent RP2040/HIL runtime
Tests/HIL/AllocatorReplay/              active HIL tests
artifacts/pilots/                        compact calibration evidence
publication/aug14_final_v1/              compact final data, analysis, and figures
docs/SIMULATION_ARCHITECTURE.md           final simulation/hardware architecture
scripts/                                AGX and Windows HIL launchers
```
