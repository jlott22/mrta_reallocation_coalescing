# Corrected experiment status

Snapshot: **2026-09-07 (America/Los_Angeles)**

This is the return point for the corrected MRTA reallocation-coalescing study.
No campaign process or service is currently running. The AGX execution is
complete; the RP2040 matrix is partially complete and must not be restarted as
a full campaign.

## Progress at a glance

| Scope | Planned | Successfully retained | Remaining | State |
|---|---:|---:|---:|---|
| AGX engineering smoke | 8 | 8 | 0 | Complete |
| AGX causal simulations | 3,000 | 3,000 technically complete | 0 | Execution complete; 60 algorithmic noncompletions retained as outcomes |
| AGX zero-compute matches | 3,000 | 3,000 | 0 | Complete |
| RP2040 hardware missions | 96 | 82 | 14 | Stopped incomplete after retry exhaustion |

The AGX export is in [`corrected_agx_v7_results/`](corrected_agx_v7_results/README.md).
The compact hardware checkpoint is in
[`corrected_hardware_v9_v10_progress/`](corrected_hardware_v9_v10_progress/README.md).
The pre-correction bundle under `publication/aug14_final_v1/` remains historical
provenance and is not an input to the corrected results.

## AGX state

All four v7 production roots completed: Round 1 and Round 2, causal and
zero-compute, with 1,500 promoted outputs per root. The tracked export contains
all 6,000 summaries, 3,000 exact causal/zero pairs, task and allocation-epoch
tables, coverage, provenance, and checksums.

The causal runs contain 60 algorithmic noncompletions (30 per round). They are
valid technical outcomes and were not discarded. The generic final aggregation
command still needs correction before publication analysis: its release-time
validator stopped at these known first blockers:

- `ACBBA__high__count_b8__trace_0002`, `task_0041`;
- `ACBBA__medium__count_b8__trace_0025`, `task_0041`.

Do not rerun AGX jobs to address this. Fix and test the analysis treatment of
retained algorithmic noncompletions against the existing export.

## RP2040 state

The fixed design remains 96 missions: four allocators, three loads, Eager and
Count B4, and four traces. V9 produced 26 successes before the heap-repair
cutover. V10 correctly excluded those 26 and produced 56 additional successes.
The 82 successful jobs and their completion hashes are retained.

V10 exhausted both allowed attempts for 14 jobs. Across all jobs it recorded 34
failed attempts: 24 parity failures, six result-chunk sequence failures, three
memory failures, and one timeout/no-response failure. Six of the 48 paired
Eager/Count blocks remain partial. Exact job IDs and failure classes are in
[`terminal_failures.csv`](corrected_hardware_v9_v10_progress/terminal_failures.csv)
and [`attempt_failures.csv`](corrected_hardware_v9_v10_progress/attempt_failures.csv).

The hardware timing lineages must stay explicit. V9 used source commit
`3fe1f578`; V10 used `a3490a77` after the common heap-fragmentation repair.
Successful outputs from those lineages may be combined for coverage, but timing
sensitivity must report lineage because garbage-collection overhead changed.

## Safe resume plan

1. Do not launch the existing `hardware_core_96.json` as a fresh campaign. Its
   v10 output root resumes safely only on this host; without that ignored root,
   the config does not independently exclude the 56 v10 successes.
2. Diagnose the recorded parity and result-stream failures without modifying
   the 82 successful result directories.
3. Seal all 82 successful job IDs in a new continuation manifest.
4. Create a new campaign/output identity that schedules exactly the 14 jobs in
   `terminal_failures.csv`, with the same fixed scenarios and policies.
5. Re-run only those 14 jobs, then rebuild the compact hardware package and run
   the descriptive hardware analysis. Failed attempts remain audit evidence.
6. Separately repair the AGX analysis validator and regenerate publication
   tables from the retained 6,000-job export; do not execute simulations again.

## Repository guide

- `AGX_CORRECTED_EXPERIMENT_HANDOFF/`: frozen design, architecture, launch
  history, and engineering audit notes.
- `corrected_agx_v7_results/`: complete compact AGX result export.
- `corrected_hardware_v9_v10_progress/`: incomplete but checksummed RP2040
  progress export and exact resume set.
- `known_visit_sim/`: corrected simulator and allocator implementations.
- `Simulation/Architecture/allocator_replay/`: native RP2040 replay runtime and
  host transport.
- `study/`: campaign scheduling, validation, and analysis code.
- `scripts/build_corrected_agx_v7_results.py`: reproducible AGX exporter.
- `scripts/build_corrected_hardware_progress.py`: reproducible RP2040 progress
  exporter.
- `publication/aug14_final_v1/`: superseded pre-correction publication bundle.

Large raw per-call hardware streams remain in ignored local output roots and
are intentionally not committed. Their paths, byte sizes, and SHA-256 hashes
are preserved in the compact hardware package's `raw_artifact_index.csv.gz`.
