# Corrected experiment handoff

This folder is the launch authority for the corrected MRTA reallocation-
coalescing experiment. Do not resume a pre-correction campaign or reuse its
manifests/output roots.

## Current execution identity

Execution is no longer active. AGX-only **v7** completed all 6,000 production
jobs and must not be touched or restarted. Hardware **v9** is sealed at 26
successful missions after the heap-failure cutover. The repaired **v10
continuation** excluded those 26 and retained 56 more, then stopped incomplete
after 14 jobs exhausted their allowed retries. The two lineages therefore hold
82/96 successful missions. Hardware `_v1` through `_v8` and failed/unpromoted
v9/v10 attempts are audit-only evidence.

See [`../CURRENT_EXPERIMENT_STATUS.md`](../CURRENT_EXPERIMENT_STATUS.md) and
[`../corrected_hardware_v9_v10_progress/`](../corrected_hardware_v9_v10_progress/README.md)
before any new launch. A future continuation must seal all 82 successes and
schedule only the 14 terminal failures; this historical v10 config is not a
fresh-clone launch authority.

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
- Engineering-only smoke: the completed 8-job AGX smoke remains separate. V10
  has no long RP2040 smoke; the brief preflight and isolated 50-task heap probe
  are the hardware gates, not matrix rows.
- Round 1 verification is informational. It must not restrict Round 2.

## Execution order

1. Clone `main` into a fresh AGX directory; do not copy old `study/output`.
   If this approved restart shares the prior disk, leave all old roots in place;
   resume AGX v7 and launch only the hardware v10 continuation.
2. Read `01_ARCHITECTURE_AND_CHANGES.md` through `07_DATA_REPLACEMENT_AND_GITHUB.md`.
3. Run the brief software/native preflight in `05_PREFLIGHT_AND_QA.md`.
4. Historical step: AGX v7 and hardware v10 have ended. Do not repeat this
   launch. Follow the safe resume plan in `CURRENT_EXPERIMENT_STATUS.md`.
5. Monitor logs/PID files; do not hold an interactive AI session open.
6. Validate and publish corrected results only after all planned stages finish.

`experiment_matrix.json` is the machine-readable design freeze. The JSON
configs in `configs/` are the only experiment configs authorized by this
handoff.
