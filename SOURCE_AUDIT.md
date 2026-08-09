# Source repository audit

Audit date: 2026-08-08 (America/Los_Angeles)

All candidate repositories were inspected read-only before this repository was
created. No live working-tree file was copied: selected content was exported
from the named Git commits with `git archive`.

## Candidates

| Repository | Commit | Initial state | Assessment |
| --- | --- | --- | --- |
| `topk_filter_study` | `90073351b6a69cb8ed870c245b058512babc74ef` | 28 pre-existing untracked analysis artifacts; porcelain SHA-256 `1a0a89ddcdf097805fe7b4e509b6af57359686947470a42bd3084b70f1c91d4f` | Selected overall donor: newest validated campaign, provenance, resumability, native RP2040 allocator, serial discovery, and HIL timing architecture. Its ignored Collaborative Visit junction and all Top-K study layers are unsuitable as-is. |
| `dcta_benchmark_sim` | `eada86b7dd8061951f2b4752c57ecdf1de95fffa` | 3 modified, 45 deleted, and 52 untracked pre-existing result/analysis entries; porcelain SHA-256 `5854409762892807833b539e33dfc40659a9741449e7637abec3a8114ed7ee00` | Selected Collaborative Visit donor: mature asynchronous 19x19/four-robot simulator and all six requested allocators. It has no HIL layer. |
| `dtca_benchmark_hardware` | `f64e4a315bc1ade4f5bccc15fb551f46c47f63a5` | One pre-existing modified ESP32 robot-ID/peer configuration; porcelain SHA-256 `13e7505973b768c533e5b2ff30bebcbd155338ff5cd32534c4cfb56540dd2444` | Not selected: useful historical physical runtime, but no Collaborative Visit lifecycle, allocator-only timing, deterministic online arrival model, tests, or current serial HIL framework. |

## Copied from clean commits

- From `dcta_benchmark_sim`: `known_visit_sim/`, its tests, deterministic
  scenarios, and minimal root metadata.
- From `topk_filter_study`: `Simulation/Architecture/allocator_replay/` and its
  HIL tests, to be refactored around unrestricted Collaborative Visit and
  reallocation-coalescing conditions.

## Intentionally not copied

- All source-repository `.git` directories, remotes, results, analysis, logs,
  caches, archives, and local uncommitted content.
- The ignored `Simulation/dcta_benchmark_sim` junction.
- Top-K campaigns, Top-K study profiles, Bayesian simulator/workflows, and
  published HIL traces/builds/results.
- The older duplicated `Hardware/Algorithms/Pololu_*.py` mission programs and
  machine-specific ESP32/network settings.

This repository is an independent real-file copy with a fresh Git history.
