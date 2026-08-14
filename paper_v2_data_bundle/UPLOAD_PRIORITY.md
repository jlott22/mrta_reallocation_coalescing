# Upload priority

## Priority 1 — always upload

- `README.md`
- `export_manifest.json`
- `data_dictionary.csv`
- `validation/BUILD_REPORT.md`
- `validation/validation_checks.csv`
- `core/trial_mechanism_summary.csv`
- `core/task_admission_reason_summary.csv`
- `core/call_epoch_trial_summary.csv`
- `core/policy_vs_eager_same_provider_extended.csv`
- `core/policy_pairwise_same_provider_extended.csv`
- `core/causal_zero_pairs_extended.csv`
- `core/cbaa_liveness_summary.csv`
- `core/cbaa_divergence_summary.csv`
- `core/hardware_call_complexity_summary.csv`

## Priority 2 — full report

- `detail/task_timing_decomposition_causal.csv.gz`
- `detail/task_timing_decomposition_zero.csv.gz`
- `detail/task_vs_eager_same_provider.csv.gz`
- all `detail/epoch_level_*.csv.gz` files
- `detail/hardware_call_level.csv.gz`
- `detail/representative_case_events.csv.gz`
- `detail/cbaa_divergence_events.csv.gz`

## Priority 3 — archival/reproducibility

- `core/raw_artifact_index.csv`
- `build_paper_v2_data_bundle.py`
- `requirements.txt`
- reconciliation and missing-data audits
- optional `detail/all_call_level_*_partNN.csv.gz` exports
