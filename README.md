# Decentralized Task-Cell Allocation Benchmark Simulator

This repository contains asynchronous grid simulators for evaluating decentralized task-cell allocation under degraded communication. It includes clue-informed search, coverage search, and collaborative visits to known target locations.

## Canonical Results

All evaluation-ready data lives under `results/`. Each study exposes one `combined/` directory with its canonical CSV files.

| Study | Scenario | Trials | Canonical location |
| --- | --- | ---: | --- |
| Clue-search core | Clue-informed target search | 75,000 | `results/clue_search_core_500/combined/` |
| Coverage core | Area coverage | 15,000 | `results/coverage_core_100/combined/` |
| Known-target visit core | Collaborative visits to known targets | 75,000 | `results/known_target_visit_core_500/combined/` |

The coverage core includes the corrected bursty Gilbert-Elliott subset: 4,800 trials, with 4,376 completed and 424 terminal safety-cap failures. GE condition IDs use the canonical `gilbert_elliott` spelling. The command-line runner also accepts the historical `gilbert_elliot` spelling as an input alias.

Sensitivity datasets are also under `results/` and are named by scenario, parameter, and trial count. The root-level `results/analysis/` directory contains reproducible analysis scripts, `tables/` for generated CSV summaries, `figures/` for generated plots, and `archive/` for superseded analysis artifacts.

Historical source paths recorded in CSV columns such as `out_dir`, `source_out_dir`, and `source_command` remain unchanged. They document where a trial originally ran; they are not live paths in this checkout.

## Repository Layout

```text
benchmark_sim/       clue-search and coverage simulator
known_visit_sim/     collaborative known-target visit simulator
scenarios/           deterministic input scenarios
results/             canonical combined datasets and analysis
  analysis/          reproducible analysis scripts and outputs
  coverage_core_100/
  clue_search_core_500/
  known_target_visit_core_500/
```

## Run A Trial

Run commands from the repository root.

```bash
python -m benchmark_sim.run_trials \
  --trial-mode clue_search \
  --algorithm benchmark_sim.algorithms.DGA:DGAAllocator \
  --algorithm-name DGA \
  --scenario-file scenarios/final_trial_500.csv \
  --max-trials 1 \
  --comm-model gilbert_elliott \
  --comm-level 0.75 \
  --out-dir results/example_clue_trial
```

For collaborative known-target visits:

```bash
python -m known_visit_sim.run_trials \
  --scenario-file scenarios/known_visit_g19_t10_n500.csv \
  --algorithm known_visit_sim.algorithms.DGA:DGAAllocator \
  --algorithm-name DGA \
  --comm-model ideal \
  --out-dir results/example_known_target_trial
```

## Verification

```bash
python -m unittest discover -s benchmark_sim/tests -v
python -m unittest discover -s known_visit_sim/tests -v
```

Python 3.10 or newer is required. `pygame` is optional and only needed for the live viewer.
