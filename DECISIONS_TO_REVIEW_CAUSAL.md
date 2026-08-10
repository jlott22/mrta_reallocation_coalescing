# Scientifically important causal decisions and assumptions

This file lists the remaining operator/reviewer decisions and the assumptions
that must be disclosed. It does not reopen the fixed architecture in the study
request. Three-board preflight and focused parity evidence now exist, but the
environment, smoke, calibration, freeze, and full-campaign gates are still
pending.

## Decisions required before design freeze

### 1. Accept one controlled AGX power/clock protocol

The environment tool is read-only. It records `nvpmodel`, `jetson_clocks`, CPU
frequency/governor, temperature, memory, and selected cores, but it does not
choose or change them. Before the first timing gate, choose a defensible mode,
apply it outside the study script if necessary, allow the AGX to stabilize, and
use the same protocol for smoke, calibration, and full execution. Then rerun
with `ACCEPT_RECORDED_POWER_CLOCK_STATE=YES`.

Decision record: __________________________________________________________

### 2. Review the common low/medium/high native rates

The calibration proposes three common rates from 0.03, 0.075, 0.15, 0.30, 0.60,
1.20, and 2.40 tasks/s. The implementation excludes processor-work outcomes
from the pressure index and requires healthy completion plus increasing
queue/compute-overlap pressure. Inspect per-algorithm diagnostics and seal one
common set; do not choose rates to make coalescing favorable.

Reviewed IDs/rates and rationale: _________________________________________

### 3. Review one common bounded timeout W

Choose among 2, 5, 10, and 20 s only after the 300-mission native pilot. The
predeclared rule requires >=95% completion and nonnegative median work saving
at medium/high load, then minimizes low-load trial-p95 completion latency. The
choice is common across allocators.

Reviewed W and rationale: _________________________________________________

### 4. Review and freeze the trace count

Twenty-five paired traces is a target, not a foregone conclusion. The n=10
causal variance pilot applies the documented exploratory half-width rule and
proposes 25 or 50. Accept the supported count or record a prospective protocol
change with `REVIEW_TRACE_COUNT_JUSTIFICATION`; arbitrary counts outside the
predeclared {25, 50} set are rejected. Do not alter n after looking at the full
results.

Reviewed n and rationale: _________________________________________________

### 5. Confirm the three-board cohort

Verify that the three selected UIDs, firmware hashes, native build/module hashes,
timer evidence, and physical connections are suitable as one timing cohort.
If a board is materially different or unstable, replace it before freeze and
repeat upstream gates. Board is a paired blocking variable, not a planned paper
factor.

Cohort decision: __________________________________________________________

## Fixed methodological assumptions to disclose

### A. One board represents four independent logical processors

One RP2040 sequentially measures four persistent robot contexts for a mission.
Every same-time input is frozen first and each virtual call completes at its
common start plus its own duration, so physical measurement order does not
serialize virtual compute. This improves throughput and preserves the intended
decentralized virtual model, but it is not four physical RP2040 processors
executing one mission. Residual board heat/GC/order effects are mitigated by
policy-order counterbalancing and diagnostics, not eliminated by the model.

Reviewer acceptance/comments: _____________________________________________

### B. AGX decisions are authoritative; RP2040 provides duration

The mission follows AGX allocator results. The device duration is accepted only
after goal, candidate/active count, call class, outbound messages, and complete
behavioral post-state parity. Raw hashes are preferred; algorithm-specific
projections bridge deliberately different desktop/native representations.
Accepting this projection as equivalently strong logical parity is a disclosed
assumption. Any mismatch fails the attempt and contributes no duration.

Reviewer acceptance/comments: _____________________________________________

### C. Timed allocator region includes policy-induced epoch reset

The causal duration is the exact sum of goal selection and the allocator's
policy-induced `on_allocation_epoch()` reset callback. It excludes
trial/context construction, generic PSETUP state/message synchronization,
explicit pre-call GC, USB, and output construction. Natural GC inside either
measured allocator operation remains included. This represents allocator
compute, not end-to-end RPC latency. Timing schema 2 cannot isolate pure host
serialization/setup; it records that field as zero and explicitly unmeasured,
while retaining transaction and host-total diagnostics.

Reviewer acceptance/comments: _____________________________________________

### D. Movement and communication remain virtual models

Movement is an explicit interval with deterministic, action-keyed jitter, but
its numerical speed is not measured from Pololu motion. Communication is ideal
with fixed 40 ms delivery and zero jitter. The study may claim a hardware-timed
allocator within a causal virtual mission, not a physical robot-system
makespan.

Reviewer acceptance/comments: _____________________________________________

### E. Paired inputs, not paired trajectories

Policies share scenario/release bytes and seeds. They may make different
decisions and trajectories because compute/coalescing changes event order. The
zero-compute condition likewise reruns the simulation rather than replaying a
stored call list. D_alloc is a causal condition-level makespan effect, not a sum
of frozen call durations; negative values remain valid and visible.

Reviewer acceptance/comments: _____________________________________________

### F. Fixed communication delay and keyed movement randomness define pairing

The 40 ms/no-jitter communication setting avoids policy-dependent consumption
of a random stream. Movement jitter is keyed by trace/runtime seed, robot,
robot-local action index, and edge; identical action identities receive the same
perturbation regardless of interleaving, while diverged paths need not align.
This is the chosen exogenous-randomness contract.

Reviewer acceptance/comments: _____________________________________________

### G. Algorithmic noncompletion guardrails are outcomes

The causal event horizon is 251,000 events and the stagnation horizon is 5,500.
Crossing either is an algorithmic incomplete outcome, not a technical failure.
It is retained and may reduce complete five-policy blocks available for
inference. Changing these thresholds after full results would require a new
prospective version.

Reviewer acceptance/comments: _____________________________________________

### H. Complete-block inference must be accompanied by the outcome audit

Friedman/Wilcoxon analysis uses only complete, hardware-valid paired blocks.
Completion, algorithmic-incomplete, excluded, retry, and technical-failure
counts must be presented alongside it so complete-case inference cannot conceal
a policy-specific failure pattern. Individual tasks are never counted as
independent replicates.

Reviewer acceptance/comments: _____________________________________________

## Fixed choices that should not be revisited during execution

- Primary algorithms: CBAA, ACBBA, PI, HIPC.
- Mission: Collaborative Visit, 19 x 19, four robots, 50 tasks, eight initial.
- Policies: Eager/B1, B2, B4, B8, bounded B4/W.
- Exactly three AGX workers and three permanently assigned RP2040 boards.
- All policies in an algorithm/load/trace block stay on one board.
- Calibration seed `20260808`; independent final seed `2026080905`.
- No Top-K, no forced trajectory equality, no post-hoc allocator-time addition.
- Technical failures retain diagnostics and bounded retries; scientific
  outcomes are never silently discarded.
