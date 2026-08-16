# Corrected AGX v7 results

This directory is the combined, AGX-only export for the corrected 50-trace
experiment. It contains 6,000 technically completed trials: 3,000 causal
host-proxy trials and their 3,000 zero-compute matches across four allocators,
three arrival loads, five policies, and 50 traces.

The export deliberately contains no RP2040 smoke, calibration, gate, or trial
artifacts. It was produced only from these allowlisted simulation campaigns:

- `corrected_round1_causal_v7`
- `corrected_round1_zero_v7`
- `corrected_round2_causal_v7`
- `corrected_round2_zero_v7`

## Files

- `trial_summaries.jsonl.gz`: complete, lossless summary JSON for all trials.
- `trial_level.csv.gz`: the same summaries in a flat analysis table.
- `allocation_epoch_level.csv.gz`: every allocation-epoch record.
- `task_level.csv.gz`: every task-event record.
- `causal_zero_pairs.csv.gz`: 3,000 exact round/job causal-zero pairs.
- `matrix_coverage.csv`: technical and algorithmic coverage by matrix cell.
- `condition_summaries.csv`: descriptive metric summaries by matrix cell.
- `completion_records.jsonl.gz`: immutable completion records and required-file hashes.
- `raw_artifact_index.csv.gz`: index of every local per-job artifact. Large diagnostic
  streams are indexed but not copied into Git because they exceed practical GitHub limits.
- `provenance/`: campaign provenance and event logs for the four AGX runs.
- `export_manifest.json`: counts, identities, sizes, and SHA-256 hashes.

The causal runs may contain algorithmically incomplete trials. They are retained
as outcomes rather than silently discarded; consult `matrix_coverage.csv` and
the per-trial `algorithmic_status` fields.

## Rebuild

From a checkout containing the ignored immutable v7 output roots, build into a
new empty path:

```bash
python3 scripts/build_corrected_agx_v7_results.py \
  --repo-root . \
  --output-dir /tmp/corrected_agx_v7_results
```

The builder fails closed if a campaign is incomplete, uses a different source
identity, contains a non-AGX timing provider, or cannot form all 3,000 exact
causal/zero pairs.
