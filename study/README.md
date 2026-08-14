# Reallocation-coalescing study campaign layer

> **Superseded campaign description.** This file documents the retained
> first-generation, post-hoc timing campaign. Its 75%-of-core worker rule,
> previously selected rates/W, and additive critical-path mission estimate do
> not govern the corrected rerun. `docs/SIMULATION_ARCHITECTURE.md` is the
> current contract. The old causal launchers, `EXPERIMENTAL_PLAN.md`, and
> `AGX_NATIVE_RUNBOOK.md` are also archived until fresh manifests, validation,
> and a new design freeze exist. Historical material below is preserved so
> earlier artifacts remain interpretable.

The corrected replacement pilot is now complete. Its current selections are
0.075/0.30/0.60 tasks per mission-second and Eager, Count B2/B4/B8, and
Bounded B4/W10. See `../PILOT_REPORT.md`; all 1.2/W5 values below remain
historical only.

This directory owns reproducible paired inputs, resumable AGX execution, and
trial-level analysis. It intentionally does not implement allocator internals.

## Design decisions and assumptions

- A trace has a 19-by-19 grid, four `edge_even` starts at `(0,0)`, `(0,6)`,
  `(0,12)`, and `(0,18)`, 50 unique task cells, and 8 tasks visible at mission
  time zero. Task IDs are one-based (`task_0001` through `task_0050`) to match
  world target numbering. These values match the requested primary design; the
  initial count was retained after a 4/8/12 pilot: it gives two initial tasks
  per robot while leaving 42 of 50 tasks online.
- Spatial, arrival, and runtime seeds are derived as separate SHA-256 streams.
  Task cells therefore cannot change when an arrival rate or policy changes.
- The 42 online releases use an exponential interarrival process placed on the
  simulator's absolute execution-time axis. Pilot D selected common
  low/medium/high rates of **0.075, 0.30, and 1.20 tasks per mission-second**.
  All loads scale the same unit-rate draws, strengthening paired comparisons.
  The axis excludes measured allocator duration; that preserves one exogenous
  trace across algorithms and policies, while the reported mission elapsed
  estimate adds allocator critical-path time separately.
- Every allocator and policy for a given trace/load receives the same scenario
  JSON, release JSON, and `runtime_seed`. Pairing never relies on implicit RNG
  call order. The child interpreter receives `PYTHONHASHSEED = runtime_seed mod
  2^32` before startup, and that value is recorded in job and completion data.
- Pure count B=2/4/8 and bounded B=4,W=5 s are separate conditions. Pilot E
  selected W=5 to cap sparse-load waiting without erasing the medium/high epoch
  reduction. The online
  runner/scheduler must flush residual pending tasks on mandatory allocation or
  terminal release-state logic; pure count batching must not deadlock.
- Analysis uses the unique Eager/B=1 row in the same
  `(algorithm, arrival_load, trial_id)` as its baseline. Task rows are explicitly
  labeled descriptive-only and are not experimental replicates.
- The worker hard cap is exactly `floor(0.75 * logical_cores)`. On the current
  22-logical-core development host it is 16. A configured or CLI worker request
  above the cap is reduced, never honored above it. Child numerical-library
  thread counts are forced to one. Execution is rejected on a single-core host,
  where no positive worker count could satisfy the cap.
- Jobs are scheduled with a SHA-256-based deterministic, condition-balanced
  round robin. The schedule seed, exact order, and order hash are recorded so
  algorithms and policies are not systematically confounded with AGX load.

## Immutable manifests

Run:

```bash
python3 -m study.manifests --config configs/agx_full.json
```

Files are generated under:

```text
study/generated/manifests/collaborative_visit_g19_t50_n25_calibrated_v2/
  manifest_index.json
  scenarios/trace_0000.json
  releases/low/trace_0000.json
  releases/medium/trace_0000.json
  releases/high/trace_0000.json
```

The index records the byte-level SHA-256 of every scenario and release file.
Generation refuses to overwrite an existing file with different content.
Release manifests redundantly carry stable task IDs/coordinates, release times,
the paired runtime seed, and the exact scenario SHA; validation cross-checks all
of them.

## Online runner contract

`study.campaign` launches one subprocess per paired condition using this
contract (the bounded timeout argument is omitted for eager/count modes):

```text
python -m known_visit_sim.run_online_trials
  --scenario-manifest PATH
  --release-manifest PATH
  --scenario-sha256 HEX
  --release-sha256 HEX
  --trial-id trace_NNNN
  --condition-id ALGORITHM__LOAD__POLICY
  --algorithm NAME
  --arrival-load LOAD_ID
  --policy-id POLICY_ID
  --policy eager|count|bounded
  --batch-size B
  [--max-pending-age-s W]
  --seed PAIRED_RUNTIME_SEED
  --output-dir ABSOLUTE_ATTEMPT_DIR
```

The runner emits `trial_summary.json`, `task_events.csv`,
`allocation_epochs.csv`, `allocator_calls.csv`, `pending_queue_samples.csv`,
and `run_metadata.json`. The first three are required for promotion. The
orchestrator executes from
the repository root, so this works from a clean, uninstalled clone. Each attempt
captures `job.json`, `stdout.log`, and `stderr.log`.

Zero exit status is not sufficient for promotion. The campaign validates a
nonempty, typed trial summary; job dimensions and manifest hashes; mission-time
arithmetic; the exact manifest task IDs, coordinates, and releases; lifecycle
timestamp/state consistency (including valid scientific noncompletion); and
epoch reasons/counts/timing. Header-only CSVs and `{}` summaries fail. Successful
results receive content hashes and `completion.json`, then move atomically to
`study/output/<campaign>/completed/<job_id>/`. Every resume rehashes every
required output and repeats semantic validation before skipping. Invalid or
conflicting completed paths are never overwritten. Failed attempts remain under
`attempts/`, with details in `failure.json`, `failures.jsonl`, and
`campaign_events.jsonl`.

The job fingerprint binds the Git HEAD and a deterministic hash of all actual
`known_visit_sim` and study Python source, including dirty/untracked source. By
default an execution from a dirty source tree is refused. `--allow-dirty` (or a
boolean `campaign.allow_dirty`) is an explicit development-pilot override; the
exact dirty source content is still fingerprinted. Generated manifest and output
roots are excluded from the dirty decision because their hashes/provenance are
tracked separately.

On Ctrl+C, queued futures are cancelled and active simulator process groups are
terminated, then force-killed after a short grace interval. Active attempts are
retained and marked failed/interrupted; completed results are untouched.

Every invocation creates an immutable provenance record containing the complete
configuration, configuration and manifest-index hashes, Git commit/dirty state,
timestamps, machine/Python metadata, logical-core count, and enforced workers.

## Timing model

The inherited asynchronous event clock accounts for movement, turns, waits,
replans, and communication, but it did **not** include measured allocator
execution. The online runner therefore retains separate timing fields:

- `simulated_execution_time_s` / `movement_time_s`: the inherited event clock;
- `cumulative_allocator_time_s`: team-serial sum of all measured allocator calls;
- `allocator_parallel_critical_path_time_s`: calls at the same logical
  epoch/timestamp are grouped, repeated calls per robot are summed, and the
  maximum robot duration is taken before groups are summed;
- `mission_elapsed_time_s`: event clock plus that four-processor critical-path
  estimate;
- `mission_elapsed_time_serial_compute_s`: event clock plus team-serial compute,
  retained as a conservative sensitivity metric;
- `host_program_runtime_s`: diagnostic only and never treated as mission time.

This is an explicit deployment-facing estimate, not a physical wall-clock
measurement. Allocator durations are not fed back into the event schedule, so
task latency timestamps remain on the exogenous event-time axis. Actual RP2040
execution is measured separately by the HIL campaign.

For causal schema-v3 output, `allocator_processor_work_s` is the neutral total
used for every timing provider. Its per-call timer includes allocator input
integration, consensus-message handling, allocator-local recovery, and
`choose_goal`; transport, decoding, PSETUP synchronization, outbound extraction,
serialization, message construction, and explicit pre-call GC are outside the
timer. RP2040-named performance fields are populated only for attested
`hardware_validated` calls; development-duration and zero-compute runs retain
only the neutral timing fields.

Schema-v3 admission events distinguish `terminal_residual` from ordinary B/W
admissions. Piggyback and `final_release_flush` compatibility counters are always
zero in new runs, while older schema outputs remain readable.

## AGX commands

From a clean clone on the AGX Orin:

```bash
bash scripts/run_agx_pilot.sh
bash scripts/run_agx_full_campaign.sh
```

Useful safe checks are `--prepare-only`, `--dry-run`, and `--job-limit N`.
`--max-workers N` may reduce concurrency; values above the detected 75% cap are
clamped. Re-running either launcher resumes without replacing completed trials.
Use `--allow-dirty` only for explicitly labeled development pilots.

After a run:

```bash
bash scripts/analyze_agx_campaign.sh configs/agx_full.json
```

## Analysis artifacts

`study/output/<campaign>/analysis/` contains:

- `trial_level.csv`: one row for every planned trial/condition, including
  technical failures or missing jobs.
- `paired_eager_deltas_trial_level.csv`: raw Eager and condition values plus
  compute saved, percent compute saved, and latency/mission/step/epoch deltas.
  This is the input level for later Friedman/Wilcoxon-Holm analysis.
- `condition_summaries.csv`: n, mean, median, sample SD, interpolated p95, min,
  max, runner success, mission completion, and paired-delta summaries.
- `task_level_descriptive_only.csv` and `allocation_epoch_level.csv`: tidy detail
  tables, not independent-replicate inputs.
- `figure1_compute_responsiveness.csv`, `figure2_mechanism.csv`, and
  `figure3_arrival_load_sensitivity.csv`: clean paper-plot inputs.
- `technical_failures_or_missing.csv` and `analysis_metadata.json`.

Positive `percent_allocator_computation_saved` means less cumulative allocator
time than Eager. Positive latency/mission/step changes mean degradation relative
to Eager. No significance claims or pseudoreplication are produced.
