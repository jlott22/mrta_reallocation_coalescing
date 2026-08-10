# MRTA Reallocation Coalescing

> **Current study path: causal native campaign.** The calibrated noncausal
> simulation/HIL workflow described later in this file is retained as historical
> evidence and is superseded for the paper experiment by
> `CAUSAL_IMPLEMENTATION_REPORT.md`, `EXPERIMENTAL_PLAN.md`, and
> `AGX_NATIVE_RUNBOOK.md`. The current design uses exactly three AGX workers and
> three RP2040 timing boards, feeds device allocator durations (`choose_goal()`
> plus any policy-induced allocation-epoch reset callback) into a causal
> four-logical-processor mission, and requires fresh native calibration plus an
> explicit design freeze. Do not launch the old `run_agx_full_campaign.sh` path
> for the causal paper.

The native causal entry sequence is:

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

Several stages deliberately require review environment variables and the first
environment check deliberately records a FAIL before acknowledgment. Follow
`AGX_NATIVE_RUNBOOK.md` rather than copying this list without its gate steps.

## Historical first-generation overview

This standalone repository implements the simulation and RP2040/Pololu HIL
study for allocator-independent coalescing of online Collaborative Visit task
arrivals. It is an isolated copy: it does not import from, modify, or require
any source repository from which components were selected.

The study compares Eager/B=1, Count B=2/4/8, and bounded B=4/W=5 s above CBAA,
ACBBA, PI, and HIPC on paired 19x19, four-robot, 50-task traces. DMCHBA and DGA
remain operational and have full-size smoke coverage. Candidate enumeration is
unrestricted; this is neither a Top-K study nor task/route bundling.

## Calibrated design

- Eight tasks are visible at time zero; 42 arrive online.
- Low/medium/high arrival rates are 0.075, 0.30, and 1.20 tasks per simulated
  mission-second.
- The full matrix is 4 allocators x 3 loads x 5 policies x 25 paired traces =
  1,500 jobs.
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

## RP2040/Pololu HIL

The HIL path is a motor-free, persistent MicroPython replay of selected
arrival/admission epochs. It times allocator goal selection and the
policy-induced allocation-epoch reset separately on the device, then reports
host/serial overhead separately. It does not reproduce robot motion or the
simulator's task-completion/idle epochs, so it validates embedded allocation
compute rather than physical mission elapsed time.

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

The generated manifests and raw campaign/HIL results are intentionally separate:
sealed final manifests are versioned in Git, while large machine-specific raw
outputs are ignored. Never infer statistical significance from task-level rows;
the paired trial is the experimental replicate.

## Repository map

```text
known_visit_sim/                         Collaborative Visit simulator
known_visit_sim/core/reallocation.py    online lifecycle and scheduler
study/                                  manifests, campaigns, validation, analysis
configs/                                calibrated AGX/HIL and retained pilot configs
Simulation/Architecture/allocator_replay/  persistent RP2040/HIL runtime
Tests/HIL/AllocatorReplay/              active HIL tests
artifacts/pilots/                        compact calibration evidence
scripts/                                AGX and Windows HIL launchers
```
