FINAL MINIMAL EXPERIMENTAL EXECUTION PLAN
MRTA Reallocation Coalescing — IPCCC Short Paper

GOAL
Complete the paper within the available time by using the AGX Orin causal simulator for the full statistical study and limiting RP2040 hardware execution to exactly 120 publication trials. Do not run large hardware calibration, hardware variance, or full-factorial hardware campaigns.

============================================================
1. FIXED STUDY SCOPE
============================================================

Mission:
- Collaborative Visit only
- 19x19 grid
- 4 robots
- 50 tasks total
- 8 initially visible
- 42 released online
- Ideal communication
- Unrestricted candidate sets
- No Top-K

Primary algorithms:
- CBAA
- ACBBA
- PI
- HIPC

Policies:
- Eager / B=1
- Count B=2
- Count B=4
- Count B=8
- Bounded B=4 with timeout W

Arrival regimes:
- Low
- Medium
- High

Paired design:
For a given algorithm, arrival load, and trace ID, every policy receives exactly the same:
- robot starts
- task coordinates
- initial visible tasks
- absolute online task-release sequence
- applicable random seeds

Policies are NOT required to make the same assignments or trajectories. Different decisions, paths, completion events, allocator-call counts, and mission durations are valid downstream effects of the policy.

============================================================
2. TIMING MODEL TO USE
============================================================

Use causal per-robot timing.

Movement:
- A robot starts a cell traversal.
- The movement duration elapses.
- Position/task completion is committed only at movement completion.
- The final cell movement duration must be included before the mission can finish.

Allocation:
- A robot reaches a safe decision boundary.
- Its allocator call starts at simulated time t.
- The call has measured duration d.
- That logical robot is compute-busy until t+d.
- The allocator decision becomes visible only at t+d.
- The robot cannot begin its next motion before t+d.
- Other robots continue independently.
- No global allocation pause.

Task releases:
- Releases remain exogenous and occur at their predetermined absolute mission times.
- A release can occur while robots are moving or computing.
- An in-flight allocator call cannot use information that arrived after its compute-start time.

For the full AGX statistical study:
- Use causal AGX allocator timing.

For selected hardware trials:
- Use measured RP2040 allocator duration as the causal compute duration.
- AGX allocation decisions remain authoritative.
- RP2040 timing is used only if the call passes the required parity check.

Mission elapsed time:
T_mission = final required task completion time - mission start time

Do not add allocator time after the mission.

============================================================
3. AGX-ONLY CALIBRATION — KEEP THIS MINIMAL
============================================================

Do NOT repeat the previous large calibration suites.

Start with the already-supported candidate design:
- low = 0.075 tasks/s
- medium = 0.30 tasks/s
- high = 1.20 tasks/s
- bounded timeout W = 5 s

Because timing is now causal, perform only a small AGX-only verification before freezing these values.

A. Arrival-regime verification
Run:
4 algorithms x 3 proposed loads x 2 policies (Eager, B4) x 3 paired traces
= 72 AGX-only trials

Verify only that:
LOW:
- arrivals are sparse
- limited coalescing opportunity
- no persistent severe backlog

MEDIUM:
- arrivals overlap regularly
- clear opportunity for coalescing
- missions remain healthy

HIGH:
- arrivals overlap heavily
- meaningful reallocation pressure
- missions remain executable rather than universally saturated

If all three regimes meet these definitions, KEEP 0.075 / 0.30 / 1.20.

Do not search for better-looking rates.

Only if one regime clearly fails its definition should adjacent rates be tested, and only for that regime.

B. Timeout verification
Run:
4 algorithms x 2 loads (low, medium) x 3 policies (Eager, B4, bounded B4/W=5) x 3 paired traces
= 72 AGX-only trials

Verify that W=5:
- limits excessive low-load task waiting
- still allows meaningful coalescing
- does not create obvious pathological behavior

If acceptable, KEEP W=5.

Only if W=5 clearly fails should W=2 and/or W=10 be tested.

C. Do NOT run a separate large variance pilot.
Use the final AGX campaign for the inferential dataset.

============================================================
4. FULL AGX STATISTICAL CAMPAIGN
============================================================

Run the complete factorial on the AGX causal simulator:

4 algorithms
x 3 arrival loads
x 5 policies
x 25 paired traces
= 1,500 AGX causal trials

Policies:
- Eager
- B2
- B4
- B8
- bounded B4/W

This is the PRIMARY inferential experiment.

Use exactly 4 AGX simulation workers/logical cores.

The AGX campaign provides:
- complete policy curves
- all 25 paired replicates
- statistical significance testing
- causal task latency
- causal mission elapsed time
- movement metrics
- event/call decomposition

No RP2040 hardware is required for B2 or B8.

============================================================
5. ZERO-COMPUTE COUNTERFACTUAL
============================================================

Run a simulation-only zero-compute counterpart for the final AGX conditions if computationally inexpensive.

For each condition:
- same algorithm
- same policy
- same load
- same trace
- same exogenous inputs
- allocator service duration set to zero

Do NOT force the same resulting trajectory.

Use this only to estimate:

D_alloc = T_causal - T_zero_compute

and:

allocation_attributable_mission_fraction
= D_alloc / T_causal

This is a secondary explanatory metric, not the headline result.

============================================================
6. HARDWARE SETUP
============================================================

Hardware:
- 1 AGX Orin
- 4 connected RP2040/Pololu boards

Run exactly 4 hardware-coupled simulations concurrently:

Simulation Worker 0 -> RP2040 A
Simulation Worker 1 -> RP2040 B
Simulation Worker 2 -> RP2040 C
Simulation Worker 3 -> RP2040 D

Each physical RP2040 belongs to one active simulation.

Each RP2040 maintains four persistent logical robot allocator contexts for that simulation.

The physical board may time logical robot calls sequentially, but this MUST NOT serialize virtual mission timing.

Example:
If logical R0, R1, R2, and R3 all start allocating at virtual time 20.0 s and measured RP2040 durations are 0.40, 0.55, 0.37, and 0.61 s, virtual completion times are:

R0 -> 20.40
R1 -> 20.55
R2 -> 20.37
R3 -> 20.61

Do NOT accumulate the physical measurement order into the virtual timeline.

============================================================
7. HARDWARE PREFLIGHT — BASIC ONLY
============================================================

Do not turn preflight into another experiment.

Before publication hardware trials, verify:

- all 4 intended boards are detected
- unique board IDs are recorded
- expected firmware/build is installed
- timer units/resolution are correct
- all 4 primary algorithms load
- four persistent logical contexts can be created/reset
- online task growth works
- AGX/RP2040 deterministic known-answer goals match
- required message/state parity check works
- USB/serial overhead is excluded from RP2040 allocator timing
- disconnect causes fail-closed termination
- no worker can use another worker's board
- same-time logical calls preserve frozen pre-call state
- context reset between missions works

No large hardware calibration is permitted.

============================================================
8. HARDWARE SMOKE TEST — BASIC ONLY
============================================================

Run no more than 8-16 total smoke missions.

The smoke set only needs to confirm:
- all 4 algorithms can complete causal hardware-timed missions
- Eager and B4 both execute
- at least low and high load are represented
- all 4 worker/board pairs are exercised
- parity remains valid
- missions complete
- no stale context/cross-board contamination occurs
- resume/restart works

Smoke trials are engineering validation only and are not part of the paper dataset.

============================================================
9. EXACT RP2040 PUBLICATION MATRIX — 120 TRIALS TOTAL
============================================================

Do not exceed this matrix unless a technical retry is required.

A. Core Eager-vs-B4 hardware validation

Run:
4 algorithms
x 3 loads
x 2 policies (Eager, B4)
x 4 paired traces
= 96 RP2040-timed causal missions

Use trace IDs 1-4 from the final sealed AGX trace set.

Purpose:
- measure actual embedded processor work
- test whether Eager->B4 compute trends transfer to RP2040 hardware
- obtain selected RP2040-timed causal task-latency and mission-time results

B. Bounded-policy hardware validation

Run:
4 algorithms
x 2 loads (low, medium)
x 1 policy (bounded B4/W)
x 3 paired traces
= 24 RP2040-timed causal missions

Use trace IDs 1-3, which must also exist in the Eager/B4 hardware subset.

Purpose:
- validate the practical bounded policy where its timeout matters most
- do not run bounded hardware trials at high load unless the final AGX data show an unexpected reason that makes them necessary

TOTAL PUBLICATION HARDWARE TRIALS:
96 + 24 = 120

With four boards running concurrently, this is 30 hardware mission slots per board if balanced.

============================================================
10. HARDWARE PAIRING / BOARD ASSIGNMENT
============================================================

For a paired block defined by:
algorithm x load x trace ID

keep the relevant policies on the same physical board whenever possible.

Example:
CBAA / medium / trace 2:
- Eager
- B4
- bounded, if applicable

should use the same board.

Counterbalance policy execution order so Eager is not always first.

Balance paired blocks across all four boards.

Record board ID for every trial.

Do not treat board identity as an experimental factor unless a real hardware problem appears.

============================================================
11. RP2040 PARITY RULE
============================================================

AGX allocator decisions remain authoritative.

For each timed RP2040 call, verify enough parity to establish that the device timed the same logical computation.

At minimum compare:
- selected goal
- active/candidate task count
- call classification
- outbound message/state signature where available

If parity fails:
- do not use that device duration
- mark the attempt as technical failure
- retain diagnostic information
- allow a bounded technical retry
- do not classify it as an algorithmic mission failure

Do not run a separate parity experiment beyond preflight/smoke and the parity checks already embedded in the 120 publication trials.

============================================================
12. PRIMARY PAPER METRICS
============================================================

A. Total allocator processor work

For one mission:

W_alloc = sum of allocator-call durations over all four logical robots

For AGX:
use AGX allocator durations.

For hardware:
use RP2040 device allocator durations.

Units:
processor-seconds

Primary paired hardware effect:

percent_processor_work_saved
= 100 * (W_Eager - W_policy) / W_Eager

Pair only identical:
- algorithm
- load
- trace ID

Interpretation:
total allocator processing required to complete the same external online workload.

Do NOT interpret this as mission delay or individual-call speed.

B. Release-to-first-assignment latency

For each task:

L_assign = first_assignment_time - release_time

Trial summaries:
- median
- p95
- mean/max as secondary diagnostics

Interpretation:
how long after a task appears before the team first decides who handles it.

C. Release-to-completion latency

For each task:

L_complete = completion_time - release_time

Trial summaries:
- median
- p95
- mean/max as secondary diagnostics

Interpretation:
how long after a task appears before it is actually serviced.

D. Mission elapsed time

T_mission
= final task completion time - mission start

Secondary paper metric.

E. Mission completion rate

All technical and algorithmic failures must remain visible and classified.

============================================================
13. MECHANISM / EXPLANATORY METRICS
============================================================

These explain WHY total processor work changed.

Record:

- total reallocation events
- arrival-driven reallocation events
- mandatory execution-driven reallocation events
- piggybacked admissions
- total allocator calls
- processor work per allocator call
- processor work per reallocation event
- processor work per completed task
- active task count per allocator call
- pending-task count/age
- max robot steps
- total team steps

Important interpretation:

A policy can reduce reallocation events but still increase total compute if its remaining calls are more expensive.

A policy can save compute but worsen mission time if it produces worse task waiting or routing decisions.

These are valid outcomes, not errors.

============================================================
14. SECONDARY TIMING METRICS
============================================================

A. Processor-capacity fraction

For four virtual processors:

processor_capacity_fraction
= W_alloc / (4 * T_mission)

Interpretation:
average fraction of total four-processor capacity consumed by allocation.

Do NOT call this percent mission time spent allocating.

B. Allocation-attributable mission fraction

Using the paired zero-compute simulation:

allocation_attributable_fraction
= (T_causal - T_zero_compute) / T_causal

Interpretation:
estimated causal contribution of nonzero allocation computation to mission makespan.

Keep secondary unless results are especially clear.

============================================================
15. STATISTICS
============================================================

Inferential statistics come from the 1,500-trial AGX dataset.

The TRIAL is the replicate.

Do NOT treat individual task rows as independent samples.

Within each:
algorithm x load

run Friedman across:
- Eager
- B2
- B4
- B8
- bounded

Primary outcomes:
- total AGX processor work
- trial-median assignment latency
- trial-median completion latency
- p95 completion latency
- mission elapsed time

If significant:
paired Wilcoxon signed-rank comparisons against Eager with Holm correction.

Also test whether the paired B4 effect changes across low/medium/high load.

RP2040 n=4 / n=3 hardware results are validation/descriptive results.

Do NOT run significance tests on the 120-trial hardware subset unless a later analysis demonstrates a clearly justified reason.

============================================================
16. EXPECTED PAPER DATA PRESENTATION
============================================================

Figure 1 — Main AGX compute-responsiveness tradeoff
For each allocator:
x = paired change in task completion latency vs Eager
y = percent total allocator processor work saved vs Eager
show Eager -> B2 -> B4 -> B8
show bounded separately
distinguish low/medium/high load

Figure 2 — Mechanism
Show:
- arrival-driven event count
- mandatory event count
- total calls
- processor work per call/event

This should explain cases such as:
fewer reallocations but no compute saving.

Figure 3 — Hardware validation
Show RP2040 Eager->B4 results across:
- four algorithms
- low/medium/high load

Primary hardware quantity:
paired percent RP2040 processor work change.

Optionally show bounded low/medium points.

Table — Selected deployment outcomes
Include representative:
- algorithm
- load
- policy
- RP2040 work change
- assignment/completion latency change
- mission-time change
- completion status

============================================================
17. EXPECTED OUTCOME BASED ON PREVIOUS PILOT
============================================================

These are hypotheses only.

Low load:
- fewer reallocation events may not save compute
- task waiting likely increases
- B4 may be counterproductive

Medium load:
- B4 likely reduces processor work
- moderate responsiveness penalty
- likely useful tradeoff regime

High load:
- eager allocation may repeatedly process closely spaced arrivals
- B4 likely provides larger processor-work savings
- extra responsiveness penalty may be relatively small

Bounded B4/W:
- intended to reduce sparse/medium-load waiting
- should approach ordinary B4 behavior when arrivals are dense

Do not tune the experiment to force these results.

============================================================
18. WHAT NOT TO RUN
============================================================

DO NOT RUN:

- 1,500 RP2040 trials
- hardware B2 trials
- hardware B8 trials
- hardware rate calibration sweeps
- hardware timeout calibration sweeps
- hardware variance/sample-size pilots
- 1-board-vs-4-board validation
- DMCHBA/DGA publication hardware campaigns
- large hardware parity studies separate from the actual calls
- physical movement validation
- communication-loss experiments
- Top-K experiments
- extra sensitivity studies unless a specific final result cannot be interpreted without one

Everything outside the 120 publication hardware trials must be either:
1. a basic safety/correctness preflight,
2. a very small smoke test, or
3. directly necessary to calculate the stated paper metrics.

============================================================
19. ORDER OF EXECUTION ON THE AGX
============================================================

1. Clone the final repository.
2. Record AGX/code/config environment.
3. Connect and identify all 4 RP2040 boards.
4. Run basic hardware preflight.
5. Run 8-16 total hardware smoke missions.
6. Run 72-trial AGX-only arrival-regime verification.
7. Run 72-trial AGX-only W=5 verification.
8. Freeze low/medium/high rates, W, manifests, policies, algorithms, and n=25.
9. Run the 1,500-trial AGX causal campaign.
10. Run zero-compute counterfactuals if retained.
11. Run the 120 RP2040 publication trials:
    - 96 Eager/B4 trials
    - 24 bounded trials
12. Run analysis.
13. Generate final campaign report and paper-facing CSVs.
14. Do not add additional experiments unless the final analysis exposes a specific unresolved scientific problem.

============================================================
20. FINAL REPORT REQUIREMENTS
============================================================

After execution, produce one concise report containing:

- final frozen rates
- final W
- AGX environment
- four board identities/builds
- smoke/preflight status
- AGX trial count completed/failed
- hardware trial count completed/failed
- technical retry count
- parity failures
- algorithmic mission failures
- primary metric summaries
- paired Eager->policy changes
- statistical test results from AGX
- descriptive RP2040 validation results
- deviations from this plan
- any result requiring further review

The intended evidence hierarchy is:

PRIMARY SCIENTIFIC EVIDENCE:
full 1,500-trial causal AGX factorial study

EMBEDDED HARDWARE VALIDATION:
120 selected RP2040-timed causal missions

ENGINEERING CHECKS:
minimal preflight + 8-16 smoke missions only

This scope is final unless a concrete technical or scientific failure makes a specific additional test necessary.
