# Corrected experiment handoff

This folder is the launch authority for the corrected MRTA reallocation-
coalescing experiment. Do not resume a pre-correction campaign or reuse its
manifests/output roots.

## Current execution identity

The active AGX-only campaign remains **v7** and must not be touched or
restarted. The repaired hardware campaign is **v9**. Its campaign ID and output
root end in `_v9`; retained hardware `_v1` through `_v8` trees are audit-only
technical evidence and must never be resumed or pooled into v9. Both campaigns
reuse the same immutable corrected scenario manifest set, not prior completion
state or analysis products.

## Fixed design

- Algorithms: CBAA, ACBBA, PI, HIPC.
- Loads: 0.075, 0.30, 0.60 tasks per mission-second.
- Policies: Eager, Count B2/B4/B8, Bounded B4/W10.
- Mission: 19x19, four robots, 50 tasks, eight initial and 42 online.
- AGX: 25 traces in Round 1 and 25 new traces in Round 2 for every condition,
  for both host-causal and zero-time treatments.
- RP2040: Eager and Count B4 only, four traces, 96 missions total. Four
  boards run one independent virtual mission each. Every mission retains four
  logical robots, but their full frozen checkpoints are reconstructed through
  one resident native runtime per board call to stay within RP2040 SRAM.
- Engineering-only smoke: the completed 8-job AGX smoke remains separate. V9
  has no long RP2040 smoke; brief preflight/late-call probes are the hardware
  gate and trace 0000 is prioritized inside the final 96 missions.
- Round 1 verification is informational. It must not restrict Round 2.

## Execution order

1. Clone `main` into a fresh AGX directory; do not copy old `study/output`.
   If this approved restart shares the prior disk, leave all old roots in place;
   resume AGX v7 and launch only hardware v9.
2. Read `01_ARCHITECTURE_AND_CHANGES.md` through `07_DATA_REPLACEMENT_AND_GITHUB.md`.
3. Run the brief software/native preflight in `05_PREFLIGHT_AND_QA.md`.
4. Keep the existing AGX v7 supervisor running. Launch
   `scripts/run_hardware_subset.sh` separately on cores 0-3 after the v9 native
   gates; it starts the final 96-mission hardware matrix directly.
5. Monitor logs/PID files; do not hold an interactive AI session open.
6. Validate and publish corrected results only after all planned stages finish.

`experiment_matrix.json` is the machine-readable design freeze. The JSON
configs in `configs/` are the only experiment configs authorized by this
handoff.
