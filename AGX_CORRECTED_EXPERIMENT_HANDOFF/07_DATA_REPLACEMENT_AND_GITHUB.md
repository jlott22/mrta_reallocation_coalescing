# Data replacement and GitHub handoff

## AGX clean start

Prefer a new clone. Do not copy any old `study/output`, old generated manifests,
old native gates, or old publication work directories into it. If reusing an
AGX disk, resolve and inspect the exact obsolete checkout/output paths before
deleting them; never run a broad recursive delete against a home directory or
workspace root.

The existing hardware `_v1` through `_v8` campaign roots are aborted execution
evidence. Preserve them in place for audit. Hardware v9 contributes only the 26
successful jobs sealed by `audit/hardware_v9_completed_26.json`; failed and
unpromoted v9 attempts remain audit-only. Do not copy v9 directories into v10.
The v10 root contributes only the 70 unfinished jobs. AGX-only v7 remains
eligible and unchanged. Preflight logs are engineering evidence, not matrix rows.

## After collection

1. Validate all expected job counts, hashes, schemas, invariants, and analysis
   products.
2. Preserve algorithmic incompletions; remove only proven technical retries or
   corrupt attempts according to the campaign's promotion rules.
3. Replace the tracked pre-correction result/publication data on GitHub `main`
   with the corrected compact data and reports in one intentional commit.
4. Do not merge aborted hardware v1-v8 or failed v9 rows into the 96 valid
   hardware rows. Preserve the v9/v10 runtime-lineage column and sensitivity.
5. Push only after the corrected output manifest and checksums validate.

The AI agent may commit/push completed phases incrementally if this reduces
loss risk, but the final publication replacement occurs only after full QA.
