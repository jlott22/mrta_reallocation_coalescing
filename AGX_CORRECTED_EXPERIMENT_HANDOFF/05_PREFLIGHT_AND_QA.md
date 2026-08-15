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
   preflight, and short targeted late-call probes using
   `configs/hardware_core_96.json`, covering:
   - dynamic delayed admission with no future-task registry;
   - one 50-task/unbounded-path transaction;
   - local and peer completion repair;
   - retained CBAA bid across movement;
   - compact/chunked large messages;
   - timer-scope attestation and host/native parity.

Use the v8 publication config for both existing native gates; do not invoke
their legacy defaults:

```bash
bash scripts/agx_native_environment_check.sh \
  AGX_CORRECTED_EXPERIMENT_HANDOFF/configs/hardware_core_96.json
# Review the first report, then explicitly accept the recorded power/clock state.
ACCEPT_RECORDED_POWER_CLOCK_STATE=YES \
  bash scripts/agx_native_environment_check.sh \
  AGX_CORRECTED_EXPERIMENT_HANDOFF/configs/hardware_core_96.json
bash scripts/agx_rp2040_preflight.sh \
  AGX_CORRECTED_EXPERIMENT_HANDOFF/configs/hardware_core_96.json
```

Do not perform a large RP calibration or a long standalone smoke. Physical
boards validate semantics, memory/transport viability, and timing scope in the
brief gates. Every later long run belongs to the fixed 96-mission publication
subset, whose first trace is scheduled first.

The hardware execution constraint is common to CBAA, ACBBA, PI, and HIPC:
four logical robot checkpoints, one native runtime resident at a time, and a
complete authoritative restore outside `W_alloc` before each call. No task or
path horizon is introduced. Strict active-state/message parity remains the
acceptance rule; only obsolete traffic for inactive tasks is suppressed, while
explicit completion releases remain sealed and compared.

For every promoted campaign require zero piggyback admissions, zero final
flushes, exact ordinary B-sized Count batches, at most one terminal residual,
message-only task knowledge, timing arithmetic consistency, and explicit
classification of incomplete missions. After Round 1, verify these properties
and then run the unrestricted Round 2 configs.
