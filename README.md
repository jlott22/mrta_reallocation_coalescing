# MRTA Reallocation Coalescing

This repository contains the corrected reallocation-coalescing experiment for
multi-robot task allocation. Superseded pre-correction data, reports, figures,
configs, and launchers have been removed from the current branch.

## Current state

- Corrected AGX execution: 3,000 causal and 3,000 exact zero-compute trials.
- Causal algorithmic outcomes: 2,940 completed missions and 60 retained
  noncompletions.
- RP2040 allocator HIL: 82 successful missions and 14 retained technical
  failures from the fixed 96-job design.
- No campaign is currently running.

The 14 hardware failures are part of the reported study denominator. An exact
retry is optional, not required before beginning analysis or writing.

Start with [CURRENT_EXPERIMENT_STATUS.md](CURRENT_EXPERIMENT_STATUS.md). Verify
the tracked data without producing analysis output:

```bash
python3 scripts/verify_current_data.py
```

## Authoritative data

- [`corrected_agx_v7_results/`](corrected_agx_v7_results/README.md): complete,
  checksummed corrected AGX export.
- [`corrected_hardware_v9_v10_progress/`](corrected_hardware_v9_v10_progress/README.md):
  checksummed 82-success/14-failure hardware checkpoint.
- `study/generated/manifests/collaborative_visit_g19_t50_n50_corrected_v1/`:
  frozen 50-trace production manifests.
- `study/generated/manifests/collaborative_visit_g19_t50_n5_corrected_pilot_v1/`:
  corrected pilot manifests.

These compact exports are the canonical inputs for future analysis. The large
raw campaign trees are intentionally not part of the repository.

## Documentation

- [`docs/SIMULATION_ARCHITECTURE.md`](docs/SIMULATION_ARCHITECTURE.md): current
  simulator and timing contract.
- [`docs/experiment/`](docs/experiment/): design freeze, experiment matrix,
  pilot record, analysis plan, and optional hardware retry instructions.
- [`docs/provenance/`](docs/provenance/): source audit, baseline regression,
  heap-repair record, and cleanup record.
- [`docs/hardware/`](docs/hardware/): passive Pololu assessment. Its motor-free
  diagnostic is not physical-motion validation.

## Code layout

- `known_visit_sim/`: corrected event-driven simulator.
- `study/`: manifests, campaign orchestration, validation, and reusable study
  utilities.
- `Simulation/Architecture/allocator_replay/`: native/RP2040 allocator replay.
- `Tests/` and `study/tests/`: simulator and HIL validation.
- `configs/corrected/`: corrected completed-run records, pilot records,
  diagnostics, and optional retry configuration.
- `scripts/`: data export/integrity and current hardware-support utilities.

No paper figures, statistical tables, or replacement publication bundle are
generated or tracked here. Those can be created later from the canonical
exports.
