# Pilot report

Pilot date: 2026-08-08 through 2026-08-09 (America/Los_Angeles)

All simulation pilots used ideal communication, unrestricted candidate sets,
paired scenario/release manifests, and at most 16 worker processes, the exact
`floor(0.75 * 22)` cap on the development machine. Timings are measured host
timings, so conclusions use paired within-campaign comparisons. Raw outputs are
ignored; compact evidence is retained under `artifacts/pilots/`.

## Pilot A: copied-source regression

The clean copied Collaborative Visit source passed its 17 original tests. One
static scenario (scenario 0, seed 9137, ideal communication) was then replayed
before and after the online changes. Coordinates, starts, completion, team/max
steps, allocator call counts, and event counts remained exact for all six
allocators:

| Algorithm | Team steps | Max steps | Calls | Events |
| --- | ---: | ---: | ---: | ---: |
| CBAA | 82 | 25 | 92 | 234 |
| ACBBA | 62 | 23 | 129 | 237 |
| PI | 68 | 23 | 107 | 227 |
| HIPC | 61 | 31 | 242 | 355 |
| DMCHBA | 92 | 27 | 101 | 272 |
| DGA | 80 | 21 | 54 | 184 |

Allocator-duration samples were present but intentionally excluded from exact
comparison because `perf_counter` measurements are nondeterministic. The
machine-readable record is `artifacts/pilot_a_static_regression.json`.

## Pilots B and C: lifecycle and scheduler semantics

Deterministic synthetic tests covered isolated/simultaneous/rapid arrivals;
Eager and Count B=2/4/8; bounded timeout; mandatory completion, invalid-goal,
and true robot-idle piggyback; arrivals during active epochs; the final partial
batch; lifecycle ordering; no pre-release service; paired determinism; and
mission-time arithmetic.

The tests exposed and fixed three copied-architecture assumptions in the new
repository only:

1. A task released at a cell traversed before release was incorrectly excluded
   by every allocator's historical `searched` set. Admission now reopens that
   cell locally while retaining physical visit/revisit history.
2. A task released under a stationary robot was not serviced because the
   movement path had length zero. It now records assignment followed by a
   post-admission, zero-motion completion.
3. A delayed peer-state message created before admission could falsely prove
   post-admission service. Peer inference now checks message creation time
   against admission time unless world truth already records completion.

An early rate-sweep output (`pilot_d_rate_sweep_v1`) preceded these fixes and
was rejected as technically invalid. It was not used for calibration or copied
into retained evidence. The corrected online suite and full simulator suite
pass 20/20 and 37/37 tests respectively.

## Pilot D: arrival-rate calibration

The corrected sweep ran 280 semantically validated jobs: seven candidate rates,
five paired traces, all four core allocators, and Eager versus Count B=4. Every
job completed all 50 tasks.

| Rate (tasks/mission-s) | Eager epochs | B4 epochs | Eager compute (s) | B4 compute (s) | Eager assignment (s) | B4 assignment (s) | B4 max queue |
| ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| 0.030 | 92.60 | 80.60 | 0.7885 | 0.8269 | 0.353 | 1.829 | 1.90 |
| **0.075 (low)** | 92.30 | 64.80 | 0.6682 | 0.7272 | 0.423 | 3.729 | 3.10 |
| 0.150 | 92.25 | 58.05 | 0.5392 | 0.5042 | 0.651 | 3.795 | 4.00 |
| **0.300 (medium)** | 92.05 | 53.70 | 0.4759 | 0.3581 | 1.375 | 3.454 | 3.95 |
| 0.600 | 92.00 | 54.45 | 0.7758 | 0.5226 | 4.972 | 6.170 | 4.00 |
| **1.200 (high)** | 92.00 | 56.05 | 1.3632 | 0.8322 | 10.042 | 9.984 | 4.00 |
| 2.400 | 92.05 | 58.40 | 1.6424 | 0.9856 | 13.616 | 14.127 | 4.00 |

The selected common rates are 0.075, 0.30, and 1.20 tasks per mission-second.
At 0.075, batching opportunities are limited and B4 can increase calls/compute;
this is retained unfavorable evidence. At 0.30 the threshold is consistently
reached while all missions remain healthy. At 1.20 overlap and allocator
pressure are strong, yet every condition completes. Rate 2.40 releases almost
the entire online set during the opening phase and was rejected as less
representative of a continuously online mission.

## Pilot E: bounded timeout

The timeout sweep ran 300 jobs: W=2/5/10/20 plus Eager, the selected three
loads, five traces, and four core allocators. All jobs completed.

At low load, mean assignment latency was 1.326, 2.221, 3.128, and 3.687 s for
W=2/5/10/20. At medium load, W=5 reduced mean epochs from 92.05 (Eager) to
56.80 and mean cumulative compute from 0.4288 to 0.3620 s, with assignment
latency rising from 1.375 to 3.112 s. At high load, B=4 usually triggered before
the timeout: W=5 produced 56.30 epochs, 0.7039 s compute, and a 3.83 s mean
maximum pending age versus 92 epochs and 1.1324 s compute for Eager.

The selected bounded point is **B=4, W=5 mission-seconds**. It gives a firm
sparse-load wait cap while retaining the mechanism at medium/high load. It was
not selected to maximize apparent compute savings; the low-load mean compute
increase remains in the results.

## Initial-task-count check

Counts 4, 8, and 12 were evaluated under medium/Eager with all four core
allocators. Every initial task and every mission completed. Four tasks produced
only 1.8-2.8 distinct eventual winning first assignees on average. Eight
improved that to 2.8-3.7 while leaving 42 online arrivals. Twelve improved it to
3.4-4.0 but makes 24% of the mission static. The final design retains **8**:
twice the robot count, an initial allocator call for every robot, and a stronger
online fraction. Eventual winning-assignee diversity is not identical to
whether each robot performed useful initial allocation work.

## Pilot F: variance and 25-trace decision

The variance pilot ran 240 jobs: ten paired traces, three selected loads, four
core allocators, and Eager versus Count B=4. All completed. Using the pilot
sample SD as a planning estimate, an exploratory two-sided 95% mean half-width
for n=25 was calculated as `t(24) * SD / 5`.

- Epoch reductions were 25.6-39.1 epochs and their projected intervals excluded
  zero for every allocator/load.
- Medium/high compute savings were clear except the small ACBBA medium effect
  (0.02385 s with a projected 0.02668 s half-width).
- Low-load compute effects were small, mixed, and sometimes unfavorable.
- Medium assignment penalties were clear; high-load assignment and completion
  differences were too variable to resolve reliably.
- Mission-time differences were highly variable and mostly projected to span
  zero.

The requested **25 paired traces** are retained because they are adequate for
the primary epoch mechanism and most medium/high compute effects in a short
paper. They are not guaranteed to resolve subtle/null mission-time or high-load
latency effects; no significance claim should be made when the final paired
uncertainty includes zero. Exact projections are in
`artifacts/pilots/pilot_f_n25_projection.csv`.

## Extended algorithms and HIL software pilot

DMCHBA and DGA each completed a full 50-task medium-load trace under Eager and
Count B=4 with unrestricted candidates. B4 reduced epochs by 38 for both;
team-serial compute fell 23.45% for DMCHBA and 31.62% for DGA. DGA remained
substantially heavier (44-65 s on the development host), which justifies its
exclusion from the 1,500-job primary matrix without removing support.

The final HIL loopback exercised all selected admission epochs through the real
chunked protocol and persistent four-context runtime. It completed 12/12
conditions and 1,168 calls, then resumed without duplication. All 1,192 journal
rows passed record/build/device-binding verification. Every visible-set-growth
call invoked the epoch hook and ran a full unrestricted solve. A compiled
loopback preflight exercised initial visibility, online growth, and duplicate
epoch idempotence for all six allocators. This is software validation only. No
RP2040 or Pololu hardware was connected, and no result is marked
hardware-validated.
