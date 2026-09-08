# Corrected study support code

This package contains the manifest, campaign, validation, causal timing, and
reusable reporting utilities used by the corrected coalescing experiment. The
active design is defined by `docs/SIMULATION_ARCHITECTURE.md` and
`configs/corrected/experiment_matrix.json`.

## Retained manifests

- `collaborative_visit_g19_t50_n50_corrected_v1`: frozen 50-trace production
  design with low/medium/high rates 0.075, 0.30, and 0.60.
- `collaborative_visit_g19_t50_n5_corrected_pilot_v1`: corrected pilot inputs.

Every allocator and policy for a trace/load uses the same scenario, release
manifest, and runtime seed. Manifest indexes bind the byte-level SHA-256 of
each input.

## Execution contract

Campaign attempts are written under ignored `study/output/`. A zero process
exit is not enough for promotion: the orchestrator validates schemas, job and
manifest identities, hashes, task lifecycles, timing invariants, and retained
algorithmic noncompletion semantics. Completed outputs are immutable and every
resume revalidates them.

The fixed corrected design uses CBAA, ACBBA, PI, and HIPC; Eager, Count B2/B4/B8,
and Bounded B4/W10; four robots; 50 tasks; and 50 paired traces. RP2040 hardware
uses the Eager/B4 subset on four boards.

The completed production configs are records under `configs/corrected/completed/`.
They are not invitations to rerun the finished AGX matrix. The only remaining
runnable publication path is the optional exact-14 hardware retry documented
in `docs/experiment/OPTIONAL_HARDWARE_RETRY.md`.

## Canonical outputs

The tracked compact exports at repository root are authoritative. Large raw
attempt trees are intentionally local-only. Run
`python3 scripts/verify_current_data.py` to check the tracked export hashes and
coverage without creating analysis output.
