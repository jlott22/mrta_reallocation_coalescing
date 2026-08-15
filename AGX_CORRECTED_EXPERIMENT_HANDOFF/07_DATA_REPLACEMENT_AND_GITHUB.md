# Data replacement and GitHub handoff

## AGX clean start

Prefer a new clone. Do not copy any old `study/output`, old generated manifests,
old native gates, or old publication work directories into it. If reusing an
AGX disk, resolve and inspect the exact obsolete checkout/output paths before
deleting them; never run a broad recursive delete against a home directory or
workspace root.

The existing `_v1`, `_v2`, `_v3`, and `_v4` campaign roots are aborted execution
evidence. Preserve them in place for audit; do not delete, rename, resume, or
copy them into a v5 campaign root. Only the `_v5` campaign roots named by this
handoff are eligible as new experimental data. Preflight logs may be kept
separately as engineering evidence but must not be pooled with the matrix.

## After collection

1. Validate all expected job counts, hashes, schemas, invariants, and analysis
   products.
2. Preserve algorithmic incompletions; remove only proven technical retries or
   corrupt attempts according to the campaign's promotion rules.
3. Replace the tracked pre-correction result/publication data on GitHub `main`
   with the corrected compact data and reports in one intentional commit.
4. Do not retain both datasets in the active publication path and do not merge
   aborted v1/v2/v3/v4 rows into v5 tables. Git history is the recovery record.
5. Push only after the corrected output manifest and checksums validate.

The AI agent may commit/push completed phases incrementally if this reduces
loss risk, but the final publication replacement occurs only after full QA.
