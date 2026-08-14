# Corrected-architecture pilot report

Pilot date: 2026-08-14 (America/Los_Angeles)

This pilot replaces every pre-correction pilot. Old rates, policy conclusions,
and pilot tables must not be pooled with or used to configure the corrected
experiment.

## Architecture under test

All retained pilot jobs used the corrected online architecture:

- tasks become allocator-visible only through authenticated environment
  messages;
- Eager admits exactly one task and Count admits exact B-sized batches;
- no completion, invalid-goal, idle, or allocator call can piggyback pending
  work;
- the sole sub-B terminal residual is admitted only after all previously
  admitted work is physically complete;
- admission is non-destructive and does not recall an executing robot;
- CBAA sees the full admitted pool but retains one task, while ACBBA, PI, and
  HIPC have no bundle cap;
- idle robots are event-driven, with robot-owned targeted recovery; and
- allocator processor time includes allocator input integration, consensus,
  recovery when invoked, and goal selection, but excludes messaging and
  transport.

The pilot used four robots, 50 tasks, eight initial tasks, ideal communication,
six rolling worker processes, host-measured causal timing, and a fresh immutable
five-trace manifest set. No RP2040 result was produced by this pilot.

## Pilot-discovered correction

An initial diagnostic run exposed a CBAA feedback loop: CBAA recomputed and
rebroadcast its retained winning bid after movement, allowing delayed old/new
messages to oscillate indefinitely. CBAA now keeps the auction-time winning bid
fixed until a real outbid, release, completion, or recovery event. The native
implementation was changed identically and regression-tested. All retained
pilot evidence was generated after this correction; pre-fix outputs are
excluded.

## Arrival-load screen

The accepted rate screen contains 200 paired Eager/Count-B4 jobs. The first
three fresh traces screened seven rates; two unused traces independently
confirmed 0.6 versus 1.2 tasks per mission-second.

| Rate | Retained jobs complete | Mean Eager epochs | Mean B4 epochs | Interpretation |
| ---: | ---: | ---: | ---: | --- |
| 0.030 | 24/24 | 43 | 12 | Too sparse and unnecessarily long |
| **0.075** | **24/24** | **43** | **12** | Selected sparse/limited-coalescing load |
| 0.150 | 24/24 | 43 | 12 | Intermediate but redundant |
| **0.300** | **24/24** | **43** | **12** | Selected overlapping-arrival load |
| **0.600** | **40/40** | **43** | **12** | Selected sustained-high load |
| 1.200 | 37/40 | 43 | 11.9 | Rejected: repeated ACBBA stagnation |
| 2.400 | 21/24 | 43 | 11.25 | Rejected: compressed arrivals and ACBBA stagnation |

The corrected Count-B4 epoch count is intentionally almost invariant: eight
initial tasks, ten exact four-task online batches, and one final two-task
terminal residual produce 12 epochs. This is evidence that the bound is now
actually enforced, not evidence that load has no effect. Load changes the wait
for those epochs, overlap with execution, allocator contention, and latency.

The selected common loads are **0.075, 0.30, and 0.60 tasks per
mission-second**. In the broader policy sweep, low and medium completed 96/96
jobs each; high completed 94/96. Both high-load incomplete jobs were ACBBA
stagnation outcomes. They remain retained as algorithmic outcomes rather than
being silently deleted.

A focused zero-time diagnostic then reran ACBBA at 0.6 across all eight pilot
policies and all three traces. It completed 24/24, including the two cells that
were incomplete under host-measured timing. The selected high load therefore
does not create a structural zero-time deadlock; it exposes sensitivity to real
compute delay and its effect on consensus event ordering.

## Policy screen

The policy screen ran 288 jobs: three selected loads, three paired traces, four
allocators, and eight policies. It compared Eager, Count B=2/4/8, and Bounded
B=4 with W=2/5/10/20 seconds. No admission piggybacking or final-release flush
occurred in any retained job.

The final five policies are:

1. Eager B=1
2. Count B=2
3. Count B=4
4. Count B=8
5. Bounded B=4, W=10 mission-seconds

Count B=2/4/8 supplies a deliberate coalescing-strength ladder. W=10 is a
better hybrid point under the corrected architecture than the old W=5 choice:

| Load | Mean total epochs | Mean timeout epochs | Timeout share of total epochs | Mean release-to-completion (s) |
| ---: | ---: | ---: | ---: | ---: |
| 0.075 | 26.67 | 24.25 | 90.9% | 21.96 |
| 0.300 | 15.00 | 7.67 | 51.1% | 29.22 |
| 0.600 | 12.33 | 2.33 | 18.9% | 34.66 |

Thus the same bounded policy is timeout-dominated when arrivals are sparse,
mixed at medium load, and threshold-dominated at high load. W=5 remained
timeout-dominated at medium load and provides less separation from Eager.

## Final design consequence

The primary factorial remains 60 conditions:

`4 allocators x 3 loads x 5 policies`.

Only two factor values change from the superseded design: high load changes
from 1.2 to 0.6 tasks/s, and Bounded B4/W5 changes to Bounded B4/W10. The
eight-initial-task and 50-task mission structure remains appropriate and was
not reopened by this pilot.

Each AGX condition will use 50 independent paired traces, staged as 25 in
Round 1 and 25 in Round 2. Round-1 verification is informational and places no
restriction on starting Round 2. The RP2040 subset remains four traces per
selected hardware condition; it is not expanded to 25 or 50.

## Recommended final performance analysis

The most informative tradeoff is **total allocator processor work versus task
completion latency, under a mission-completion constraint**. Coalescing can
reduce admission epochs and allocator work, but tasks wait longer before they
are eligible and execution/consensus ordering can change. No one scalar should
combine these effects.

Bounded B4/W10 is the pilot's best balanced policy to evaluate, not a declared
universal winner. Its aggregate pilot means stayed near Eager responsiveness
while moving from timeout-dominated to threshold-dominated behavior as load
increased. Count B4 is the useful more-aggressive reference; B2 and B8 show the
shape on either side.

Against Eager in the three-trace aggregate, W10 showed the following
descriptive tradeoff (not an inferential estimate):

| Load | Allocator-work change | Release-to-completion change | W10 completion | Eager completion |
| ---: | ---: | ---: | ---: | ---: |
| 0.075 | -4.9% | +33.5% | 12/12 | 12/12 |
| 0.300 | -4.3% | +15.4% | 12/12 | 12/12 |
| 0.600 | -1.7% | +5.1% | 12/12 | 11/12 |

Count B4 was more aggressive: at low load it saved 10.8% work but increased
completion latency 135.7%; at medium it increased both work and latency; at
high it saved 10.2% work while increasing latency 14.4%. This is why the final
matrix should estimate a Pareto frontier rather than rank policies by one
average score.

Use this outcome hierarchy:

1. **Completion/liveness:** mission completion indicator and structured
   algorithmic failure type. Report failure rates before success-only means.
2. **Primary efficiency:** total allocator processor work per trial. Keep AGX,
   zero-time, and hardware-provider results separate; never label a proxy as
   RP2040 performance.
3. **Primary responsiveness:** per-trial mean release-to-completion latency,
   plus per-trial p95 release-to-completion latency for tail behavior.
4. **System outcome:** mission elapsed time and total/max robot steps.
5. **Mechanism:** admission-epoch count, threshold/timeout/terminal-residual
   mix, allocator-call count, per-call duration, and logical allocation bytes.
6. **Latency decomposition:** release-to-admission,
   admission-to-first-current-goal, and first-current-goal-to-completion where
   derivable.

Treat each trial as the replicate and use paired trace-level contrasts against
Eager within `(allocator, load, trace)`. Analyze allocator and load interactions
rather than relying on one pooled mean. Raw first-plan claims, bundle length,
and reassignment churn are not fair primary comparisons: CBAA owns one task,
whereas ACBBA/PI/HIPC expose full-path claims and use different suffix/item
repair rules.

## Measurement caveat

Host-measured causal trials are not bitwise replays: measured allocator
duration affects simulated event ordering, so OS scheduling or processor
contention can alter a later consensus trajectory. This is part of the causal
compute treatment, but it requires dedicated AGX cores, no worker
oversubscription, paired manifests, and distributional analysis over 25+25
traces. The zero-time trials isolate architecture/policy behavior without this
host-timing path. Absolute host timings from separate campaigns must never be
compared as if they were hardware-calibrated RP2040 measurements.

Compact evidence is retained in `artifacts/pilots/`; raw campaign outputs are
local, ignored, and must not be promoted as publication data.
