# Corrected experiment status

Snapshot: **2026-09-08 (America/Los_Angeles)**

No campaign process is running. The corrected AGX experiment is complete. The
fixed RP2040 allocator-HIL study is retained as 82 successful missions and 14
technical failures; those failures remain in the study denominator rather than
being treated as missing data.

## Progress

| Scope | Planned | Successful | Retained failures/noncompletions | State |
|---|---:|---:|---:|---|
| AGX causal | 3,000 | 2,940 algorithmically complete | 60 algorithmic noncompletions | Execution complete |
| AGX zero-compute | 3,000 | 3,000 | 0 | Complete |
| RP2040 allocator HIL | 96 | 82 | 14 technical failures | Complete as observed; retry optional |

The AGX export is [`corrected_agx_v7_results/`](corrected_agx_v7_results/README.md).
The hardware checkpoint is
[`corrected_hardware_v9_v10_progress/`](corrected_hardware_v9_v10_progress/README.md).

All 82 successful hardware missions passed host/device parity and completed
algorithmically. The 14 terminal failures comprise 11 ACBBA, two PI, and one
CBAA job; nine are high-load, three medium-load, and two low-load. Across the
34 failed attempts, the recorded categories are 24 parity failures, six result
chunk-sequence failures, three memory failures, and one timeout/no-response.
A failed attempt can carry more than one category.

## Exact hardware failures

| Job ID | Recorded categories |
|---|---|
| `ACBBA__high__trace_0000__count_b4` | parity; result chunk sequence |
| `ACBBA__high__trace_0000__eager_b1` | memory; parity |
| `ACBBA__high__trace_0001__count_b4` | result chunk sequence |
| `ACBBA__high__trace_0002__count_b4` | parity |
| `ACBBA__high__trace_0002__eager_b1` | memory |
| `ACBBA__high__trace_0003__count_b4` | parity; result chunk sequence |
| `ACBBA__high__trace_0003__eager_b1` | parity; timeout/no response |
| `ACBBA__low__trace_0002__eager_b1` | parity |
| `ACBBA__medium__trace_0001__count_b4` | parity |
| `ACBBA__medium__trace_0003__count_b4` | parity; result chunk sequence |
| `ACBBA__medium__trace_0003__eager_b1` | parity |
| `CBAA__high__trace_0001__count_b4` | parity |
| `PI__high__trace_0001__count_b4` | parity |
| `PI__low__trace_0003__eager_b1` | parity |

The authoritative machine-readable records are
[`terminal_failures.csv`](corrected_hardware_v9_v10_progress/terminal_failures.csv)
and [`attempt_failures.csv`](corrected_hardware_v9_v10_progress/attempt_failures.csv).
Failed attempts are evidence, never successful result rows.

## Interpretation and optional retry

The hardware campaign measures allocator execution and parity on four RP2040
boards. It is not a moving-robot experiment. V9 contributed 26 successes before
the heap repair and V10 contributed 56 afterward; timing lineages must remain
explicit.

The study can be analyzed and written with 82 successes and 14 reported
failures. If more hardware time becomes available, follow
[`docs/experiment/OPTIONAL_HARDWARE_RETRY.md`](docs/experiment/OPTIONAL_HARDWARE_RETRY.md).
The retry configuration schedules exactly the 14 jobs above and refuses to
rerun any of the 82 successes.

## Remaining work

1. Verify the retained data with `python3 scripts/verify_current_data.py`.
2. Perform the statistical analysis described in
   [`docs/experiment/ANALYSIS_PLAN.md`](docs/experiment/ANALYSIS_PLAN.md).
3. Decide whether the optional 14-job hardware retry is worth the time; it is
   not required to retain or report the failures.
4. Write the paper while clearly separating AGX, zero-compute, allocator-HIL,
   and any future physical-motion evidence.
