# Optional RP2040 retry validation checklist

The corrected hardware study is already complete as observed at 82 successes
and 14 reported technical failures. Use this checklist only if choosing to
retry those exact failures.

- [ ] Work from a clean, committed `main` checkout.
- [ ] `python3 scripts/verify_current_data.py` passes.
- [ ] `configs/local/agx_board_bindings.json` identifies four distinct boards,
  serial endpoints, build IDs, firmware hashes, and module-set hashes.
- [ ] `scripts/agx_native_environment_check.sh` produces a passing environment
  gate for this commit and board binding.
- [ ] `scripts/agx_rp2040_preflight.sh` produces a passing parity preflight for
  the same commit and board cohort.
- [ ] The dry-run schedule contains exactly 14 jobs and no sealed success.
- [ ] Heap, chunk transport, and high-load ACBBA probes are reviewed before a
  long retry.
- [ ] `RUN_OPTIONAL_HARDWARE_RETRY=YES` is set only after the checks above pass.
- [ ] All failed retry attempts remain under the new v11 output root.
- [ ] Any successful retry output is labeled v11 and does not overwrite the
  immutable v9/v10 checkpoint or its failure tables.
