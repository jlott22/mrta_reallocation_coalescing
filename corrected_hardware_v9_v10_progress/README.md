# Corrected RP2040 hardware progress: v9 + v10

This is the compact, checksummed observed-result package for the corrected
96-job RP2040 matrix. It accounts for 82 successful missions and 14 retained
technical failures. Those failures are part of the study denominator; retrying
them is optional.

- V9 contributes 26 successful trials sealed before the heap repair.
- V10 contributes 56 successful trials after the heap repair.
- Together they retain 82/96 successful trials; 14 v10 jobs exhausted both
  allowed attempts and are reported as terminal technical failures.
- V10 did not schedule any of the 26 successful v9 trials.

The two source/build lineages must remain explicit in timing analyses because
the v10 heap repair can change garbage-collection overhead without changing
allocator decisions. Do not pool their timing fields without a lineage
sensitivity analysis.

## Files

- `trial_summaries.jsonl.gz`: lossless summaries for all 82 successful trials.
- `trial_level.csv.gz`: the same summaries in a flat table.
- `completion_records.jsonl.gz`: immutable completion records and required-file
  hashes for the 82 successful trials.
- `matrix_coverage.csv`: planned, v9-complete, v10-complete, and failed counts
  for each algorithm/load/policy cell.
- `condition_summaries.csv`: descriptive summaries of successful trials only.
- `terminal_failures.csv`: the 14 jobs that still require resolution.
- `attempt_failures.csv`: all 34 failed v10 attempts, including failures that
  later succeeded on retry.
- `raw_artifact_index.csv.gz`: sizes and SHA-256 identities for successful raw
  outputs and failure records retained locally but omitted from Git.
- `provenance/`: immutable schedules, campaign journals, tracker snapshots, and
  the sealed v9-success manifest.
- `export_manifest.json`: package counts, identities, file sizes, and hashes.

## Optional retry rule

Do not restart the 82 successful trials. Any future hardware continuation must
seal those exact job IDs and schedule only the 14 jobs in
`terminal_failures.csv`. The exact allowlisted path is documented in
`../docs/experiment/OPTIONAL_HARDWARE_RETRY.md`. Failed attempts are audit
evidence, never successful result rows.

## Rebuild

Run this from a checkout that still contains the ignored immutable v9 and v10
output roots:

```bash
python3 scripts/build_corrected_hardware_progress.py \
  --repo-root . \
  --output-dir /tmp/corrected_hardware_v9_v10_progress
```

The builder verifies both campaign schedules and source identities, hashes all
completion-sealed artifacts, checks the v9 success manifest, and proves that
the 82 successes plus 14 failures partition the fixed 96-job design.
