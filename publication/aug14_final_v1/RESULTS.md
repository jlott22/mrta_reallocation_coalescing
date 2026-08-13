# Final MRTA reallocation-coalescing results

Generated from the completed August 14 experiment matrix. Raw per-call and
per-epoch artifacts remain in the local immutable campaign trees; this folder
contains compact analysis-ready exports and exact checksums.

The exporter requires Python 3.10+ and the packages listed in
`requirements.txt`.

## Dataset

- Primary causal trials: **3000** (2968 completed; 32 legitimate algorithmic noncompletions).
- Matched zero-compute trials: **3000** (2999 completed; 1 legitimate algorithmic noncompletions).
- Exact causal/zero pairs: **3000**; both missions completed for **2967** pairs.
- Hardware-backed causal trials: **104**; parity passed for **104/104**.
- Separate verification trials: **144**.
- Retained technical attempt failures: **3**, all recovered;
  unresolved technical failures: **0**.

## Main findings

- Across completed pairs, causal allocation changed mission time by a mean of
  **-0.024 s** (95% CI
  **[-0.534, 0.485]**),
  with median **0.003 s** and paired
  effect size *d*<sub>z</sub> **-0.002**.
- The smallest median causal-minus-zero mission difference was under
  **`eager_b1`** (0.001 s); the
  largest was under **`count_b4`** (0.018 s).
- Mean first-assignment latency changed by **0.121 s**
  and mean task-completion latency by **0.347 s**.
- The RP2040 used a median **26.749×** the
  allocator processor work measured on AGX across the hardware-backed trials.
- All hardware trials passed output parity. The verification matrix observed
  arrival-driven eager triggers and bounded-wait timeout triggers in every
  applicable trial.

### Coalescing policies relative to Eager

These causal medians use exact allocator/load/trace pairs. Positive latency or
mission values are slower than Eager; positive call savings mean fewer calls.

| Policy | Mission change (s) | Assignment-latency change (s) | Allocator calls saved |
|---|---:|---:|---:|
| `count_b2` | 0.074 | 1.032 | 34.0 |
| `count_b4` | 0.480 | 2.054 | 44.5 |
| `count_b8` | -0.265 | 2.566 | 57.0 |
| `bounded_b4_w5` | 0.278 | 1.631 | 58.0 |

Algorithmic noncompletions are included in completion-rate denominators and in
`legitimate_noncompletions.csv`; they are omitted only from statistics that
mathematically require a completed mission time.

## Export guide

- `data/primary_trial_level.csv`: one row per causal or zero-compute trial.
- `data/causal_zero_paired_trial_level.csv`: exact paired comparisons.
- `data/primary_condition_summary.csv`: 60 conditions per timing dataset.
- `data/paired_condition_summary.csv`: paired inference for all 60 conditions.
- `data/paired_factor_summary.csv`: overall and factor-level effects.
- `data/policy_vs_eager_paired_trial_level.csv` and
  `policy_vs_eager_summary.csv`: policy tradeoffs against exact Eager pairs.
- `data/task_condition_summary.csv`: task-level descriptive aggregation.
- `data/hardware_trial_level.csv` and `hardware_summary.csv`: device timing validation.
- `data/verification_trial_level.csv` and `verification_summary.csv`: engineering checks.
- `data/legitimate_noncompletions.csv`: explicit censored outcome audit.
- `data/execution_audit.csv`: recovered technical attempts and historical
  validation-record classification by campaign.
- `figures/`: publication-ready PDF figures.
- `publication_manifest.json`: file hashes, matrix counts, and generation metadata.

Confidence intervals are two-sided 95% Student-*t* intervals over trial-level
paired differences. `paired_condition_summary.csv` includes paired *t*,
Wilcoxon signed-rank, Cohen's *d*<sub>z</sub>, and Benjamini-Hochberg adjusted
mission-effect values. Task-level exports are descriptive and do not treat
tasks as independent experimental replicates.
