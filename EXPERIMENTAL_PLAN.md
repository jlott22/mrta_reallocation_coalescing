# Experimental plan: causal reallocation coalescing

## 1. Research question

How does reallocation coalescing change total computational workload and task
responsiveness in online multi-robot task allocation, including downstream
changes it induces in allocation decisions and mission execution?

The estimand is a paired mission-level policy effect under common exogenous
inputs. It is not a frozen-call replay effect. Policies may make different
decisions, call the allocator different numbers of times, travel different
paths, and finish at different times.

## 2. Hypotheses

The primary hypotheses are evaluated separately by allocator and arrival load.

- H1 (mechanism): count/bounded coalescing changes arrival-driven reallocation
  frequency and therefore total RP2040 allocator processor work relative to
  Eager.
- H2 (responsiveness): stronger coalescing changes trial-level
  release-to-assignment and release-to-completion latency relative to Eager.
- H3 (workload dependence): the paired B4-versus-Eager work effect varies over
  low, medium, and high arrival pressure.
- H4 (deployment consequence): nonzero device compute changes causal makespan
  relative to the exact zero-compute condition.
- H5 (bounded compromise): one common B4/W setting can limit sparse-load waiting
  while retaining nonnegative median work saving at medium/high load.

These are scientific questions, not assertions about result direction. The
prior noncausal pilot motivates the workload-dependent hypothesis but does not
constrain the causal result.

## 3. Experimental unit, block, and factors

The independent replicate is one complete mission/trace condition. The primary
paired block is:

`algorithm x arrival_load x trace_id`

All five policies in a block use the same scenario bytes, absolute release
manifest, runtime seed, final-cohort seed derivation, and physical RP2040 board.
The 50 task rows inside a mission are not independent replicates.

Independent variables:

- allocator: CBAA, ACBBA, PI, HIPC;
- arrival load: native-calibrated low, medium, high;
- coalescing policy: Eager/B1, B2, B4, B8, bounded B4/W; and
- compute counterfactual for deployment analysis: native RP2040 duration versus
  zero duration under the same condition identity.

Board is a blocking/provenance variable, not a planned paper factor. If native
diagnostics show material board differences, report them and perform a
sensitivity analysis rather than silently pooling incompatible devices.

## 4. Controlled mission inputs

The primary mission is Collaborative Visit on a 19 x 19 grid with four logical
robots and 50 tasks. Eight tasks are visible at time zero and 42 are released
online. Candidate enumeration is unrestricted: no Top-K filtering or task/route
bundling is used.

Controlled properties include:

- the same four robot starts within a trace;
- identical task IDs and coordinates within a trace;
- identical eight initially active tasks;
- identical absolute release timestamps for the 42 online tasks;
- identical runtime and applicable stochastic seeds;
- ideal communication with a fixed 0.040 s delivery delay and zero jitter;
- the same virtual movement-time model; and
- common rates, W, trace count, firmware/module build, and analysis version for
  every allocator.

The final cohort uses master seed `2026080905`, independently from the
calibration cohort seed `20260808`. Freeze validation requires no calibration/
final trace signature overlap. Exogenous pairing does not imply identical
decisions or trajectories.

## 5. Timing treatment

Each mission is a four-logical-processor causal simulation. At an eligible safe
control boundary, a robot freezes its pre-call state, the AGX computes the
authoritative decision, and the bound RP2040 measures the parity-equivalent
allocator duration `d_r`: goal selection plus any policy-induced allocation-
epoch reset callback. That logical robot cannot move or publish the
staged result until virtual time `t+d_r`. Other robots continue independently.

For a same-time call group, every pre-call state is frozen before the first call
result can become visible. Sequential physical requests on the one board are
measurement order only. Each call completes at the common virtual start plus
its own duration.

Absolute task releases, scheduler timeouts, movement completions, and message
deliveries remain in the event queue while any robot is compute-busy. New
information cannot retroactively change an in-flight computation. The next
eligible allocator invocation processes buffered information according to the
allocator/scheduler protocol.

Movement is an action interval. Position and any arrival-serviced task are
committed at `movement_start + movement_duration`. `mission_elapsed_time_s` is
the timestamp of the final required task completion, measured from mission
start, for a completed mission. It is neither summed robot work nor host wall
time. An incomplete mission has no defined final-completion makespan; retain its
horizon/event clock as a diagnostic and exclude it from makespan inference.

The RP2040 allocator duration is the checked sum of separate goal-selection and
policy-induced allocation-epoch-reset timers. USB, generic state/message setup,
explicit pre-call GC, and output serialization are excluded. Natural GC inside
either measured allocator operation is included. AGX call time, RP2040 call time, protocol
transaction wall times, device pre-call setup, host preparation, and host total
time remain separate raw diagnostics. See `CAUSAL_IMPLEMENTATION_REPORT.md` for
the schema-2 limitation: pure host serialization cannot be isolated and is
explicitly marked unmeasured.

## 6. Policies and event decomposition

The fixed policy set is:

1. Eager / B=1;
2. Count B=2;
3. Count B=4;
4. Count B=8; and
5. Bounded Count B=4 with common timeout W.

Pending arrivals are admitted when a count or age condition fires. A mandatory
allocation may piggyback pending tasks before the count threshold. Residual
pending work is flushed by terminal release-state logic so count batching does
not deadlock.

Mechanism analysis separates arrival-driven policy-controlled events from
mandatory/execution-driven events and retains exact trigger reasons:
`initial_allocation`, `task_arrival_eager`, `batch_threshold`, `age_timeout`,
`final_release_flush`, `task_completion`, `invalid_goal`, `robot_idle`,
consensus/internal, and other. Piggybacked admissions are counted explicitly.

## 7. Hardware and execution design

One AGX Orin runs exactly four worker processes. Worker i owns one permanent,
exclusive RP2040 serial session and executes one mission at a time. Its board
hosts four persistent logical allocator contexts during that mission. This is
one timing board per mission, not one physical processor per logical robot.

The four workers are pinned to four distinct configured logical cores. Relevant
numerical-library thread counts are one. Stable device UID and sealed
build/firmware/module hashes replace `/dev/ttyACM*` enumeration as identity.
Four workers must fit within 75% of the detected logical-core count, but the
publication concurrency is always four.

All five policies in a paired block remain on one board. A crossed Latin board
assignment balances algorithm, load, and trace across four boards. A per-board
cyclic Latin policy rotation counterbalances order. The deterministic hash
schedule and exact worker/core/board mapping are sealed.

## 8. Native calibration protocol

Calibration is required because the old post-hoc rates and timeout are not
final causal settings. It must run only after the environment check, twenty-item
native preflight, and causal smoke pass.

### 8.1 Smoke

The smoke contains four algorithms, Eager and B4, two provisional sparse/heavy
loads, and two traces: 32 missions. It must use every board/worker, finish with
clean parity and invariants, observe causal compute behavior, and pass a second
content-validated resume invocation.

### 8.2 Arrival-rate calibration

Candidate rates are 0.03, 0.075, 0.15, 0.30, 0.60, 1.20, and 2.40 tasks per
mission-second. The matrix is:

`7 rates x 4 algorithms x 2 policies (Eager, B4) x 5 traces = 280 missions`

One common low/medium/high set is selected across algorithms. Eligible endpoints
must have at least 95% mission completion. The predeclared pressure index uses
B4 median pending depth, B4 batch-threshold events per 42 online tasks, and the
fraction of online releases strictly inside compute intervals. Processor work
does not enter rate selection. Low/high are the lowest/highest healthy endpoints
only if pressure increases; medium is the healthy interior rate closest to the
pressure midpoint. Labels are relative to this candidate sweep. The operator
must inspect per-algorithm diagnostics and explicitly seal all three choices.

### 8.3 Bounded-timeout calibration

After rates are reviewed, test W = 2, 5, 10, and 20 s with Eager and the four
bounded candidates:

`3 loads x 4 algorithms x 5 conditions x 5 traces = 300 missions`

The common-W rule first keeps candidates with at least 95% completion and
nonnegative median RP2040 work saving versus Eager over paired medium/high
trials. Among those, choose the candidate with the smallest median low-load
trial-p95 release-to-completion latency. Work is a feasibility constraint, not
an objective to maximize. W is not tuned per allocator.

### 8.4 Variance and n check

Run:

`3 loads x 4 algorithms x 2 policies (Eager, B4) x 10 traces = 240 missions`

For each algorithm/load cell, summarize paired RP2040 work, percent saving,
median assignment latency, median and p95 completion latency, mission elapsed
time, and event-count effects. The implemented exploratory rule carries the
n=10 sample SD to n=25 using `2.064 * SD / sqrt(25)`. Retain n=25 when at least
75% of algorithm/load cells have a projected half-width no larger than the
observed absolute mean for both primary work and completion-latency effects;
otherwise recommend 50. Zero effects and missing variance are inadequate. This
is a planning heuristic, not a confirmatory power calculation; the operator
must explicitly review n.

## 9. Design freeze and final matrix

Do not run the final campaign directly after calibration. The operator first
reviews every native report and invokes the explicit freeze. The freeze must
bind exactly six passing evidence kinds: environment, preflight, smoke, rate
calibration, timeout calibration, and variance pilot.

The target frozen matrix, if n=25 remains supported, is:

`4 algorithms x 3 loads x 5 policies x 25 traces = 1,500 native-timed missions`

Any reviewed n other than 25 changes the final count and must be reported as a
protocol deviation supported by the variance report. The frozen config cannot
silently regenerate rates, W, n, manifests, schedule identity, code identity,
or device cohort.

## 10. Zero-compute counterfactual

After the causal matrix completes, run an equivalent zero-duration condition
for every frozen algorithm/load/policy/trace identity. It reuses the same
scenario, release manifest, policy, algorithm, and random inputs; it does not
reuse a stored allocator-call sequence or force the native trajectory.

Changing compute duration can change event ordering, later calls, assignments,
and paths. The pairing is therefore condition-level:

```text
D_alloc = T_RP2040_causal - T_zero_compute
F_alloc = D_alloc / T_RP2040_causal
```

`F_alloc` is the allocation-attributable mission fraction under this causal
model, not the sum of a frozen call trace. Negative D values are retained and
flagged.

## 11. Outcomes and equations

### 11.1 Processor work

For mission m:

```text
W_rp2040(m) = sum over calls c of d_device(c)
W_agx(m)    = sum over calls c of d_agx(c)
```

Both are processor-seconds. They are reported separately and are not mission
delay. The primary paired work effect for policy P is:

```text
S_work(P) = 100 * [W(Eager) - W(P)] / W(Eager)
```

Positive S means work saved.

### 11.2 Task responsiveness

For each task j:

```text
L_assign(j)   = first_assignment(j) - release(j)
L_complete(j) = completion(j) - release(j)
```

Within each trial report median and p95 latency (and descriptive mean/max).
The trial summary, not each task, enters inferential analysis. Admission-to-
eligible, admission-to-assignment, and assignment-to-completion components are
retained to explain mechanisms.

### 11.3 Mission and capacity

```text
T_mission = final required task completion - mission start
C_alloc   = W_rp2040 / (4 * T_mission)
```

`C_alloc` is the aggregate fraction of the modeled four-processor capacity
consumed by allocator computation. Also retain total and maximum per-robot
movement steps, calls, reallocation events, trigger counts, queue summaries,
and work per call/event/completed task.

### 11.4 Outcome status

Report planned, technically completed, algorithmically completed,
algorithmically incomplete, excluded, retried, and permanently failed counts.
Never treat a parity/transport fault as a mission outcome, never silently omit
an unfavorable completed mission, and never substitute a duration after a
failed call.

## 12. Statistical analysis

All confirmatory comparisons use trial-level paired blocks. For each
`algorithm x load`, run a Friedman test across Eager, B2, B4, B8, and bounded
B4/W for:

- RP2040 processor work;
- trial-median assignment latency;
- trial-median completion latency;
- trial-p95 completion latency;
- causal mission elapsed time;
- max robot steps; and
- total team steps.

Only complete five-policy blocks enter a Friedman test. If the omnibus result
is significant at the declared alpha, run paired Wilcoxon signed-rank contrasts
against Eager and apply Holm correction within the declared family. Report raw
and adjusted p-values, n, effect direction, and paired effect magnitude.
Deterministic paired bootstrap 95% intervals are used where implemented for
work saving, latency/makespan effects, D_alloc, capacity consequences, and
steps. Do not reinterpret task rows as additional n.

Workload dependence is shown with paired B4 effects across the three loads,
including `Delta W_B4 = W_B4 - W_Eager` and relative work saved. Completion and
technical-outcome tables accompany inferential results so complete-case
analysis cannot hide failure patterns. No significance is fabricated when
pairing/coverage is incomplete.

## 13. Paper-facing outputs

- Figure 1: x = paired release-to-completion latency change; y = paired percent
  RP2040 work saved; Eager -> B2 -> B4 -> B8 trajectory, bounded B4/W separate,
  loads distinguished.
- Figure 2: arrival-driven and mandatory events, piggybacks, total calls, and
  RP2040 work per call/event by load/policy.
- Figure 3: causal mission-time change, processor-capacity fraction,
  allocation-attributable fraction, and max/total movement steps. Split panels
  if one graphic would obscure the result.
- Table: compact algorithm/load/policy summary with sample/completion status,
  work, task latency, mission elapsed time, and key mechanism counts.

Every figure/table must be traceable to immutable analysis input hashes and the
frozen analysis version.

## 14. Interpretation rules

- Same exogenous input does not mean identical decision or trajectory.
- Fewer events do not imply less processor work; inspect work per call/event.
- Summed robot compute is processor work, not makespan.
- USB or setup time is not allocator computation.
- A positive responsiveness delta means slower task service relative to Eager;
  a positive percent-work-saved value means less work.
- A negative D_alloc is possible through trajectory/event-order changes and is
  reported, not clipped.
- Board identity is a block. If results drift by board/order/temperature, show a
  sensitivity analysis and describe the deviation.
- Results that contradict the prior sparse/medium/heavy narrative are retained
  and become the paper result.

## 15. Limitations and validity threats

- Movement is modeled virtual motion, not measured Pololu travel.
- One physical board sequentially samples four logical processors. Frozen
  snapshots and independent virtual completions prevent semantic
  serialization, but thermal/GC/order effects in the physical measurement
  stream may remain; counterbalancing and per-call diagnostics mitigate them.
- AGX and compact native allocator representations require algorithm-specific
  behavioral projections when raw hashes cannot be identical. Preflight and
  mutation tests make that projection fail closed, but this remains a
  methodological assumption to disclose.
- Communication is idealized as fixed 40 ms delivery without jitter.
- Goal selection and the policy-induced allocator epoch-reset callback form the
  timed deployment region. Generic state synchronization and output
  construction are excluded by design and reported separately.
- Pure host serialization cost cannot be isolated by timing schema 2; its field
  is explicitly unmeasured.
- AGX authoritative timing is recorded under four pinned workers, so power,
  clocks, temperature, and throttling must be controlled/reported even though
  RP2040 time drives the virtual mission.
- Algorithmic horizon outcomes can make complete-block inference unavailable;
  completion/outcome patterns must remain visible.
- Twenty-five traces is a target until the native causal variance stage is
  reviewed and frozen.

## 16. Reproducibility record

The final review package consists of the frozen design/config, manifest index
and release hashes, four-board binding, environment and preflight reports,
calibration reports, causal/zero schedules and execution reports, every
promoted raw job plus retained technical failures, analysis metadata and input
hashes, and `FULL_CAMPAIGN_REPORT.md`. The exact execution order is documented
in `AGX_NATIVE_RUNBOOK.md`.
