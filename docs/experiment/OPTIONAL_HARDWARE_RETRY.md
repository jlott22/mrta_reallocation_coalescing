# Optional retry of the 14 hardware failures

The current study result is 82 successful RP2040 allocator-HIL missions and 14
retained technical failures. A retry is optional. Reporting those failures is
scientifically valid provided the denominator and failure concentration are
clear.

## Safety contract

`configs/corrected/hardware_optional_retry_14.json` is bound to the tracked
hardware checkpoint by SHA-256. Its loader verifies that:

- the checkpoint contains 82 unique successful jobs;
- `terminal_failures.csv` contains 14 unique retry jobs;
- the two sets do not overlap; and
- together they exactly partition the original 96-job Eager/B4 design.

The scheduler constructs the original full schedule before filtering, so each
retry retains its original board/worker mapping and policy order. The new
campaign identity is `corrected_hardware_core_96_v11_optional_retry_14`.

## Commands

From a clean AGX checkout:

```bash
python3 scripts/verify_current_data.py
bash scripts/agx_native_environment_check.sh
bash scripts/agx_rp2040_preflight.sh
python3 -m study.causal.orchestrator \
  --repo-root . \
  --config configs/corrected/hardware_optional_retry_14.json \
  --dry-run
RUN_OPTIONAL_HARDWARE_RETRY=YES bash scripts/run_optional_hardware_retry.sh
```

If the boards must be rebuilt first, use
`scripts/agx_prepare_rp2040_boards.sh` with four stable serial paths, then rerun
the environment and preflight gates.

Do not copy old `study/output/` trees into this checkout. Never rename v11
successes as v9/v10 results, and never delete the original terminal or attempt
failure records.
