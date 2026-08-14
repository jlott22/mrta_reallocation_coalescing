# Corrected experiment handoff

This folder is the launch authority for the corrected MRTA reallocation-
coalescing experiment. Do not resume a pre-correction campaign or reuse its
manifests/output roots.

## Fixed design

- Algorithms: CBAA, ACBBA, PI, HIPC.
- Loads: 0.075, 0.30, 0.60 tasks per mission-second.
- Policies: Eager, Count B2/B4/B8, Bounded B4/W10.
- Mission: 19x19, four robots, 50 tasks, eight initial and 42 online.
- AGX: 25 traces in Round 1 and 25 new traces in Round 2 for every condition,
  for both host-causal and zero-time treatments.
- RP2040: Eager and Count B4 only, four traces, 96 missions total. Four
  boards run one independent virtual mission each; every mission still has four
  persistent logical robot contexts on its assigned board.
- Engineering-only smoke: one 8-job AGX smoke and one 8-mission RP2040 smoke.
  Neither belongs to the inferential matrices.
- Round 1 verification is informational. It must not restrict Round 2.

## Execution order

1. Clone `main` into a fresh AGX directory; do not copy old `study/output`.
2. Read `01_ARCHITECTURE_AND_CHANGES.md` through `07_DATA_REPLACEMENT_AND_GITHUB.md`.
3. Run the brief software/native preflight in `05_PREFLIGHT_AND_QA.md`.
4. Start the background supervisors with `scripts/start_background.sh`. The
   simulation supervisor runs the 8-job AGX smoke before the 6,000-job matrix;
   when hardware is enabled, the hardware supervisor runs the 8-mission
   RP2040 smoke before the 96-mission subset.
5. Monitor logs/PID files; do not hold an interactive AI session open.
6. Validate and publish corrected results only after all planned stages finish.

`experiment_matrix.json` is the machine-readable design freeze. The JSON
configs in `configs/` are the only experiment configs authorized by this
handoff.
