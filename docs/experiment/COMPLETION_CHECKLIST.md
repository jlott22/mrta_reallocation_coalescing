# Corrected experiment completion checklist

## Completed evidence

- [x] Corrected architecture and frozen 50-trace manifests retained.
- [x] Corrected AGX engineering smoke completed.
- [x] Round 1 causal and zero-compute: 1,500 jobs each.
- [x] Round 2 causal and zero-compute: 1,500 jobs each.
- [x] All 3,000 causal/zero pairs are present and checksummed.
- [x] All 60 causal algorithmic noncompletions are retained as outcomes.
- [x] RP2040 study accounts for all 96 planned jobs: 82 successes and 14
  terminal technical failures.
- [x] All 82 successful hardware jobs passed parity and completed
  algorithmically.
- [x] V9 and post-heap-repair V10 runtime lineages remain explicit.
- [x] Superseded pre-correction data, reports, figures, configs, and launchers
  removed from the current branch.

## Ready for analysis and writing

- [x] Hardware failures remain in the denominator and have machine-readable
  attempt records.
- [x] The study may be analyzed and written with the observed 82/96 hardware
  success rate; retrying failures is optional.
- [x] Canonical compact inputs pass `scripts/verify_current_data.py`.
- [ ] Statistical analysis completed by the researcher.
- [ ] Paper figures and tables created from the canonical exports.
- [ ] Physical-motion evidence added if claimed; the present hardware study is
  allocator HIL only.

## Optional hardware retry

- [ ] Four current board bindings and fresh environment/preflight gates exist.
- [ ] Exact-14 retry config passes a dry run.
- [ ] Retry is explicitly enabled by the operator.
- [ ] Any new successes are retained as v11 lineage without deleting the
  original failure audit.
