# Brief preflight and QA

Before launch:

1. Confirm a clean committed checkout and at least 12 logical cores.
2. Run the three software suites and Python compilation once.
3. Generate the fresh 50-trace manifest set with a campaign `--prepare-only`
   or dry run; confirm every config resolves the same manifest hashes.
4. Run the engineering-only 8-job AGX smoke
   (`configs/agx_smoke_8.json`): all four allocators, Eager/B4, medium load,
   and trace 0000.
5. If boards are present, run only the existing environment check, native
   preflight, and the engineering-only 8-mission RP2040 smoke
   (`configs/rp2040_smoke_8.json`) covering:
   - dynamic delayed admission with no future-task registry;
   - one 50-task/unbounded-path transaction;
   - local and peer completion repair;
   - retained CBAA bid across movement;
   - compact/chunked large messages;
   - timer-scope attestation and host/native parity.

Use the corrected smoke config for both existing native gates; do not invoke
their legacy defaults:

```bash
bash scripts/agx_native_environment_check.sh \
  AGX_CORRECTED_EXPERIMENT_HANDOFF/configs/rp2040_smoke_8.json
# Review the first report, then explicitly accept the recorded power/clock state.
ACCEPT_RECORDED_POWER_CLOCK_STATE=YES \
  bash scripts/agx_native_environment_check.sh \
  AGX_CORRECTED_EXPERIMENT_HANDOFF/configs/rp2040_smoke_8.json
bash scripts/agx_rp2040_preflight.sh \
  AGX_CORRECTED_EXPERIMENT_HANDOFF/configs/rp2040_smoke_8.json
```

Do not perform a large RP calibration. The smoke outputs are explicitly
engineering-only and non-inferential. Physical boards validate semantics,
memory/transport viability, and timing scope before the fixed 96-mission subset.

For every promoted campaign require zero piggyback admissions, zero final
flushes, exact ordinary B-sized Count batches, at most one terminal residual,
message-only task knowledge, timing arithmetic consistency, and explicit
classification of incomplete missions. After Round 1, verify these properties
and then run the unrestricted Round 2 configs.
