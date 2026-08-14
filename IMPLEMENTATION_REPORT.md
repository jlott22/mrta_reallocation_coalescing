# Implementation report

> **Historical pre-correction report.** This describes the earlier post-hoc
> design; `CAUSAL_IMPLEMENTATION_REPORT.md` describes the subsequent but still
> pre-correction causal campaign. The current implementation contract is
> `docs/SIMULATION_ARCHITECTURE.md`. Rates/W/n must be freshly calibrated and
> frozen before a corrected full run. The historical record below is otherwise
> intentionally unchanged.

Date: 2026-08-09 (America/Los_Angeles)

## Outcome

This repository is ready for the calibrated AGX simulation campaign and for a
software-validated, hardware-pending RP2040/Pololu embedded-compute campaign.
It implements an allocator-independent scheduler above Collaborative Visit;
tasks remain independent tasks, and no Top-K/candidate restriction is active.

The final primary design is four robots on a 19x19 grid, 50 tasks (8 initially
visible and 42 online), four primary allocators, three calibrated arrival loads,
five policies, and 25 paired traces: 1,500 simulation jobs. DMCHBA and DGA remain
operational and passed full-size smokes. The selected HIL subset contains 300
conditions (four allocators, three loads, five policies, and five traces).

## 1-4. Source selection, isolation, and copied material

Three candidate repositories were inspected read-only before this repository
was created. Exact commits and pre-existing dirty states are recorded in
`SOURCE_AUDIT.md`.

- `topk_filter_study` at `90073351...` was the best overall architecture for
  campaign provenance/resume logic, native persistent RP2040 execution,
  transport, device timing, and HIL deployment. Its study-specific Top-K and
  Bayesian layers were not suitable.
- `dcta_benchmark_sim` at `eada86b7...` supplied the actual Collaborative Visit
  simulator and current CBAA, ACBBA, PI, HIPC, DMCHBA, and DGA implementations.
- `dtca_benchmark_hardware` at `f64e4a31...` was inspected but not selected. It
  has valuable historical motion firmware, but no Collaborative Visit online
  lifecycle, simulator, deterministic release manifests, common scheduler, or
  allocator-only per-call timer.

Clean committed content was exported with `git archive`; no live dirty file was
copied. This repository was created as a real sibling directory with its own
`.git`, not a branch, worktree, junction, or symlink. It contains no import or
path dependency on the donor repositories.

Copied material:

- `known_visit_sim/` and deterministic scenario support from the simulation
  donor;
- `Simulation/Architecture/allocator_replay/` and its relevant native/physical
  HIL tests from the campaign/HIL donor;
- minimal root metadata needed for a standalone checkout.

Intentionally excluded material:

- donor `.git` directories, remotes, results, logs, caches, archives, and all
  uncommitted content;
- the donor's ignored simulator junction and all local cross-repository paths;
- Top-K experiment profiles/results, Bayesian campaign routes, candidate caps,
  and legacy device modules from the deployable bundle;
- older monolithic motor/sensor programs, MQTT credentials, ESP32 IDs, and
  machine-specific network/serial settings.

At the final safety checkpoint, all three donor HEADs and their complete Git
status listings matched the recorded initial state. Their pre-existing changes
remain the user's changes and were neither cleaned nor committed.

## 5-8. Scheduler and allocation-epoch semantics

`known_visit_sim/core/reallocation.py` contains the common policy and scheduler;
allocator internals remain responsible for normal decisions and consensus.
Every online task has an explicit event history through:

`unreleased -> released -> pending -> admitted -> assigned -> completed`.

Release and pending are recorded at the same exogenous timestamp, but remain
distinct events. Admission controls visibility to every allocator. A new-task
epoch calls every logical robot once before movement resumes and then allows
the original algorithm's internal calls/messages to continue normally.

One allocation epoch is opened for each concrete trigger:

- `initial_allocation` at time zero;
- `task_arrival_eager` for one or more releases sharing a timestamp under B=1;
- `batch_threshold` when pending count reaches B;
- `age_timeout` when the oldest pending task reaches W;
- `final_release_flush` for a pure-count residual batch at the final release;
- mandatory `task_completion`, `invalid_goal`, or true `robot_idle` events.

`consensus/internal` calls attach to the responsible active epoch; they neither
fabricate epochs nor admit pending tasks. Epoch closure means every expected
robot has made its first allocator call, not that distributed consensus has
mathematically converged. Per-call rows retain epoch ID and trigger reason.

If a mandatory event occurs while tasks are pending, all pending tasks are
admitted in that already-required epoch, marked `piggybacked_pending=true`, and
the epoch becomes global. Without pending work, a completion remains one source
epoch and local invalid-goal/idle work remains scoped to the affected robot.
This preserves necessary mission allocation while coalescing arrival work.

The scheduler invokes a common allocation-epoch hook only when the visible set
grows. It invalidates stale path/claim state without changing scoring,
candidate enumeration, communication impairment, commitment horizons, or the
algorithms' ordinary completion behavior. DMCHBA/DGA keep their required
algorithm-specific reset semantics. Candidate mode is always `unrestricted`
and `max_candidate_cells` is always null on the active runner.

## 9-13. Arrival model, policies, and algorithms

The sealed set
`study/generated/manifests/collaborative_visit_g19_t50_n25_calibrated_v2/`
contains 25 scenario JSON files and 75 release JSON files. Each scenario has
four edge-even starts `(0,0)`, `(0,6)`, `(0,12)`, `(0,18)` and 50 unique task
cells sampled without replacement from non-start cells. IDs are one-based
`task_0001` through `task_0050`.

Master seed `20260808` is split with SHA-256 into independent spatial, arrival,
and runtime streams. The 42 online interarrival values are drawn once from
Exp(1); low/medium/high divide the same draws by their common rates. Releases
are cumulative absolute timestamps, predetermined before any allocator runs:

- low: **0.075 tasks per simulated mission-second**;
- medium: **0.30 tasks per simulated mission-second**;
- high: **1.20 tasks per simulated mission-second**.

The absolute axis is the inherited simulation execution clock, independent of
algorithm and policy. It excludes measured allocator duration; this pairing
choice and its interpretation are flagged below.

Supported policies are configurable, not hard-coded:

- Eager B=1;
- Count B=2, B=4, and B=8;
- bounded B=4, W=5 mission-seconds.

Pure count cannot deadlock: a mandatory event piggybacks pending work, and the
last exogenous release explicitly flushes any residual batch. That tail event is
named and logged rather than being disguised as a threshold hit.

CBAA, ACBBA, PI, and HIPC are in the primary matrix. DMCHBA and DGA remain
selectable in simulation and HIL without source edits. DGA was not put in the
primary 1,500-job matrix because its retained unrestricted genetic search was
orders of magnitude heavier in the full-size smoke, not because support or an
unfavorable result was removed.

## 14-17. Regression, scheduler tests, pilots, and bugs fixed

The copied simulator initially passed 17/17 source tests. Static exact
regression after all online changes remained identical for all six allocators;
the exact team/max steps, calls, and event counts are in
`artifacts/pilot_a_static_regression.json`. The final simulator suite passes
37/37, the manifest/campaign/analysis suite 21/21, and the active HIL suite
33/33. Pilot detail is in `PILOT_REPORT.md`.

Completed calibration work:

- Pilot A: exact static regression across all six algorithms.
- Pilots B/C: deterministic lifecycle, threshold, timeout, mandatory
  piggyback, final flush, timestamp, paired replay, and arithmetic cases.
- Pilot D: 280 corrected jobs across seven rates, five traces, four algorithms,
  and Eager/B4; selected 0.075/0.30/1.20.
- Pilot E: 300 jobs across four W candidates, three loads, five traces, and four
  algorithms; selected W=5.
- Pilot F: 240 jobs over ten traces for variance assessment; retained the
  requested 25 paired final traces with explicit limitations.
- Initial task pilot: 4/8/12; retained 8.
- Extended smoke: DMCHBA and DGA, medium Eager/B4, four full 50-task jobs.
- HIL software pilot: 12/12 conditions, 1,168 accepted allocator calls and a
  clean resume; 1,192 journal rows passed record/build/device-binding hashes.

Important defects found and fixed only in this new repository:

1. Internal/consensus polling opened hundreds of false epochs and bypassed B by
   piggybacking pending work. Only concrete triggers can now open epochs.
2. One world completion could generate duplicate peer completion epochs. It now
   produces one logical source epoch.
3. Pre-release traversal permanently disqualified later tasks in all allocators.
   Admission now reopens the local cell but retains world visit history.
4. A task admitted under the robot produced a zero-length path failure. It now
   receives an assignment and zero-motion post-admission completion.
5. A delayed pre-admission state message could falsely complete a new task.
   Message creation time is now checked against admission.
6. DGA online RNG was not explicitly trace-paired. It now derives a stable
   trace-specific stream without changing static regression behavior.
7. An early generated manifest set used zero-based task IDs while current code
   expected one-based IDs. It was rejected, never overwritten, and replaced by
   the sealed calibrated v2 set.
8. Empty JSON/header-only CSV runner output could be promoted as completed.
   Promotion now performs typed, cardinality, lifecycle, hash, dimension, and
   mission-arithmetic validation.
9. Resume trusted old file hashes and omitted source identity. It now rehashes
   outputs and fingerprints Git HEAD, relevant source content, config,
   manifests, and Python hash seed.
10. Manifest/output path traversal, nondeterministic hash seeding, fixed
    condition order, and interrupt child leakage were hardened with containment,
    `PYTHONHASHSEED`, balanced scheduling, and process-group termination.
11. HIL admission initially grew the active list without notifying native
    allocator caches. A once-per-robot idempotent epoch hook now forces a real
    unrestricted solve on visible-set growth for all six allocators.
12. HIL schedules/results were not bound to live firmware/build/module identity,
    auto-discovery could probe unrelated serial ports, and preflight did not
    exercise online growth. All three paths are now fail-closed.

An early, invalid rate pilot was retained only as ignored diagnostic state and
was not used for calibration. No unfavorable scientific trial was discarded.

## 18-21. Timing definition

The inherited simulator did **not** provide deployment wall-clock mission time.
Its asynchronous event clock (`clock_s`) accounted for modeled movement,
quarter-turns, no-goal waits, replans, collision settling/backoff, and
communication, while allocator calls ran synchronously on the host without
advancing that clock. Raw process runtime also includes simulator orchestration,
logging, and file I/O and is therefore not a mission metric.

The new fields are:

- `simulated_execution_time_s` and `movement_time_s`: the inherited event clock;
- `other_execution_time_s`: zero because the inherited clock cannot support a
  defensible movement/other decomposition without double counting;
- `cumulative_allocator_time_s`: team-serial sum of every measured
  `choose_goal()` duration;
- `allocator_parallel_critical_path_time_s`: for each epoch/timestamp group,
  sum repeated calls per robot, take the maximum robot duration, then sum the
  group maxima;
- `mission_elapsed_time_s = simulated_execution_time_s +
  allocator_parallel_critical_path_time_s`;
- `mission_elapsed_time_serial_compute_s = simulated_execution_time_s +
  cumulative_allocator_time_s`, a conservative sensitivity field;
- `host_program_runtime_s`: diagnostic only.

This avoids incorrectly adding four logically parallel RP2040 processors, but
it remains a model-based estimate. Allocator durations are not fed causally back
into wake/release scheduling, and release/assignment/completion timestamps stay
on the simulation event-time axis. Automated arithmetic invariants cover both
runner and campaign promotion. This is the most important interpretation choice
for user review.

No step count is relabeled as seconds. The copied model explicitly assumes a
1.60 s cell move with seeded +/-0.10 s jitter bounded to 1.50-1.70 s, 0.30 s per
quarter turn, 0.30 s replan delay, 0.50 s no-goal delay, 0.10 s collision-intent
settling, and configured communication delay. These are simulation parameters,
not measured Pololu kinematics.

For HIL, the RP2040 timer encloses `choose_goal()` only. State restore/setup,
USB transport, serialization, and host journaling remain separate overhead
fields. HIL does not claim physical mission elapsed time.

## 22. AGX launch instructions

The active simulation/campaign path requires Python 3.10+ and only the standard
library. From a clean committed clone on the AGX Orin:

```bash
bash scripts/run_agx_pilot.sh
bash scripts/analyze_agx_campaign.sh configs/agx_pilot.json
bash scripts/run_agx_full_campaign.sh
bash scripts/analyze_agx_campaign.sh configs/agx_full.json
```

Use `--prepare-only`, `--dry-run`, or `--job-limit N` for safe checks. A dirty
full checkout is refused; `--allow-dirty` exists only for explicitly labeled
development pilots. Rerunning resumes valid content-hashed completions and
never overwrites a conflicting result.

The launcher enforces `floor(0.75 * logical_cores)` regardless of a larger
configuration/CLI value and forces child numerical-library thread counts to
one. On the 22-logical-core development system all simulations used no more
than 16 workers. Job order is deterministic and condition-balanced to reduce
algorithm/policy correlation with machine load. Provenance captures config,
manifest index, Git/source identity, seed, exact order, timestamp, Python,
platform, hostname, and core/worker counts; failures remain inspectable.

## 23. HIL launch instructions

On Windows, connect the intended RP2040/Pololu boards and run:

```powershell
$env:PYTHONPATH=(Resolve-Path "Simulation/Architecture").Path
python -m pip install -r Simulation/Architecture/allocator_replay/requirements-host.txt
scripts/run_hil_pilot.ps1 -DryRun
scripts/run_hil_pilot.ps1 -Ports COM12,COM13,COM14,COM15 -PrepareDevices
scripts/run_hil_campaign.ps1 -Ports COM12,COM13,COM14,COM15 -RunPreflight
```

Safe auto-discovery probes only USB VID:PID `2e8a:0005`, the official Pico
MicroPython CDC identity, and records/skips every other serial port. An explicit
port list is the operator allow-list for boards enumerating differently.
Deployment does not write `main.py`, initialize motors, or initialize sensors.

Preflight performs live `HELLO`/`CHECK`, verifies module-set bytes against the
selected build, binds firmware/device identity, then tests two initially visible
tasks, online growth to three, and duplicate-epoch idempotence for all six
allocators. The campaign rechecks identity before every trial. Schedule,
journal, completion, and report rows are sealed to the same binding.

The HIL experiment is a motionless admission-epoch replay. The full 50-task
universe is resident, only admitted tasks are active, and each of four persistent
logical contexts is called once per admission epoch (one configured round). It
does not synthesize movement/completion/idle epochs; pure-count tail flush is
explicitly labeled. Join it to simulation only with paired manifest hashes.

## 24. Output files and schemas

Each simulator job emits:

- `trial_summary.json`: dimensions, hashes, completion, steps, execution/compute
  timing, call/epoch counts, latency summaries, queue statistics, and trigger
  counts;
- `task_events.csv`: task ID/location and release, admission, first assignment,
  first robot, completion, completing robot, assignment-event count, all five
  derived latencies, and serialized lifecycle history;
- `allocation_epochs.csv`: trigger, mandatory/piggyback flags, pending depth/age,
  admitted IDs/count, expected/called robots, call IDs, and epoch allocator time;
- `allocator_calls.csv`: call/robot/mission time, duration, epoch, and trigger;
- `pending_queue_samples.csv`: event-sampled depth and oldest age;
- `run_metadata.json`: command, Git, machine/Python, and summary hash.

The campaign wraps these with `job.json`, stdout/stderr, immutable
`completion.json`, attempt/failure records, event journal, and run provenance.
Analysis produces `trial_level.csv`, paired Eager deltas,
`condition_summaries.csv`, descriptive task/epoch tables, technical failures,
metadata, and clean Figure 1/2/3 CSVs. Positive compute saved means less than
the paired Eager row; positive latency/mission/step deltas mean degradation.

HIL produces `schedule.json`, `config_snapshot.json`, `state.json`, append-only
`journal/attempts.jsonl`, `allocator_calls.csv`, `trial_metrics.csv`,
`condition_metrics.csv`, and `summary.json`. Rows distinguish device allocator
time from host non-allocator/transport overhead and include the sealed device
binding plus `hardware_validated` status.

## 25-26. Final verification and hardware-only work

Verified on Python 3.13.14/Windows:

- simulator: 37/37 tests passed;
- campaign/manifests/analysis: 21/21 passed;
- active HIL: 33/33 passed;
- final manifest set regenerated byte-identically and hash-validated;
- AGX full dry-run planned jobs with an enforced 16/22 worker cap;
- HIL calibrated pilot dry-run: 12/12, 1,168 calls, clean resume;
- compiled loopback preflight: all six allocators, online growth and duplicate
  epoch checks passed;
- DMCHBA/DGA full-size smoke: 4/4 completed and resumed without duplication;
- no candidate restriction violations and no hardware-validation claim.

The remaining hardware-only work is to connect the intended four boards, build
and deploy for their reported MicroPython compatibility, pass live preflight,
run the selected campaign, and inspect actual device clock/firmware identity.
No physical device was opened during implementation.

## 27-28. Interpretation and decisions requiring attention

The study uses ideal communication, preserving the selected source condition;
it does not claim a communication-impairment interaction. Pending queue means
are event-sampled rather than time-weighted. Epoch first-call completion is not
consensus convergence. Concurrent AGX `perf_counter` timings can contain OS,
DVFS, and thermal noise despite deterministic balancing. Task rows are
descriptive, never independent replicates. These and all parameter choices are
expanded in `DECISIONS_TO_REVIEW.md`.

## USER REVIEW REQUIRED

- Accept the deployment-facing critical-path mission-time estimate and
  event-time release axis, or require a causal compute-delay extension before
  launching the full campaign.
- Confirm 8 initial tasks and 25 paired traces for the short-paper scope.
- Choose and record AGX power/clock/thermal settings; consider a lower-worker
  timing sensitivity run.
- Complete the physical four-board preflight before citing any HIL value.
