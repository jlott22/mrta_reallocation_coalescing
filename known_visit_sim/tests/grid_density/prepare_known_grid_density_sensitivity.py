#!/usr/bin/env python3
"""
Prepare scenario files and a condition manifest for the known-target visit
robot-density/grid-size sensitivity study.

Designed to be saved in:
  /home/jlott/dcta_benchmark_sim/known_visit_sim/tests/grid_density/prepare_known_grid_density_sensitivity.py

Run indirectly through run_known_grid_density_sensitivity.sh, or directly from repo root:
  python3 known_visit_sim/tests/grid_density/prepare_known_grid_density_sensitivity.py --repo-root /home/jlott/dcta_benchmark_sim
"""
from __future__ import annotations

import argparse
import csv
import random
from pathlib import Path
from typing import Dict, List, Sequence, Tuple

try:
    from known_visit_sim.config import edge_even_start_positions, generate_robot_ids
except Exception:  # pragma: no cover - helpful error on wrong repo/package state
    edge_even_start_positions = None
    generate_robot_ids = None

Cell = Tuple[int, int]

# Same grid/density matrix used for the clue/coverage scaling tests.
GRIDS: List[int] = [14, 19, 25, 34, 48]
TARGET_DENSITIES: List[int] = [220, 180, 140, 110, 85, 65, 50]

# Exact planned robot counts. Columns are TARGET_DENSITIES in order.
ROBOT_COUNTS: Dict[int, Dict[int, int]] = {
    14: {220: 1, 180: 1, 140: 2, 110: 2, 85: 3, 65: 3, 50: 4},
    19: {220: 2, 180: 2, 140: 3, 110: 4, 85: 5, 65: 6, 50: 8},
    25: {220: 3, 180: 4, 140: 5, 110: 6, 85: 8, 65: 10, 50: 13},
    34: {220: 6, 180: 7, 140: 9, 110: 11, 85: 14, 65: 18, 50: 24},
    48: {220: 11, 180: 13, 140: 17, 110: 21, 85: 28, 65: 36, 50: 46},
}

COMM_ENVS = [
    # label, comm_model, comm_level
    ("ideal", "ideal", "1.0"),
    ("bernoulli_drop_0_10", "bernoulli", "0.1"),
    ("gilbert_elliott_0_90", "gilbert_elliott", "0.9"),
    ("rayleigh_sens_-59_4", "rayleigh_style", "-59.4"),
]

# Known-visit simulator algorithm registry names from test_known_visit.py.
ALGORITHMS = [
    ("cbaa", "CBAA"),
    ("acbba", "ACBBA"),
    ("pi", "PI"),
    ("hipc", "HIPC"),
    ("dmchba", "DMCHBA"),
    ("dga", "DGA"),
    ("auctiongreedy", "AuctionGreedy"),
]


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo-root", default="/home/jlott/dcta_benchmark_sim")
    parser.add_argument("--run-root", default=None, help="Default: <repo-root>/results/sensitivity_known_target_visit_grid_density_50")
    parser.add_argument("--num-trials", type=int, default=50)
    parser.add_argument("--num-targets", type=int, default=10, help="Keep this at 10 for the known-target benchmark.")
    parser.add_argument("--scenario-seed", type=int, default=20260701)
    parser.add_argument("--sim-seed-base", type=int, default=810000)
    return parser.parse_args()


def planned_start_cells(grid_size: int) -> set[Cell]:
    """Return every edge_even start used by any planned density at this grid size."""
    starts: set[Cell] = set()
    if edge_even_start_positions is not None and generate_robot_ids is not None:
        for robot_count in set(ROBOT_COUNTS[grid_size].values()):
            starts.update(edge_even_start_positions(grid_size, generate_robot_ids(robot_count)).values())
        return starts

    # Fallback mirrors the edge-even convention if the package import is unavailable
    # during preparation. The worker will still use the real package function.
    for robot_count in set(ROBOT_COUNTS[grid_size].values()):
        if robot_count == 1:
            starts.add((0, (grid_size - 1) // 2))
            continue
        for index in range(robot_count):
            y = round(index * (grid_size - 1) / (robot_count - 1))
            starts.add((0, int(y)))
    return starts


def generate_known_target_scenarios_for_grid(
    grid_size: int,
    num_trials: int,
    num_targets: int,
    seed: int,
    out_path: Path,
) -> None:
    """Generate deterministic 10-target known-visit CSVs for one grid size."""
    if num_targets <= 0:
        raise ValueError("num_targets must be positive")

    rng = random.Random(seed + grid_size * 1009 + num_targets * 17)
    forbidden = planned_start_cells(grid_size)
    available = [
        (x, y)
        for y in range(grid_size)
        for x in range(grid_size)
        if (x, y) not in forbidden
    ]
    if len(available) < num_targets:
        raise ValueError(f"grid {grid_size} has only {len(available)} non-start cells for {num_targets} targets")

    header = ["trial_id"]
    for i in range(1, num_targets + 1):
        header.extend([f"target{i}_x", f"target{i}_y"])

    out_path.parent.mkdir(parents=True, exist_ok=True)
    with out_path.open("w", newline="") as f:
        writer = csv.writer(f)
        writer.writerow(header)
        for trial_id in range(num_trials):
            targets = rng.sample(available, num_targets)
            row: List[object] = [trial_id]
            for x, y in targets:
                row.extend([x, y])
            writer.writerow(row)


def main() -> None:
    args = parse_args()
    repo_root = Path(args.repo_root).resolve()
    run_root = Path(args.run_root).resolve() if args.run_root else repo_root / "results" / "sensitivity_known_target_visit_grid_density_50"
    scenario_dir = run_root / "scenarios"
    raw_dir = run_root / "raw"
    combined_dir = run_root / "combined"

    scenario_dir.mkdir(parents=True, exist_ok=True)
    raw_dir.mkdir(parents=True, exist_ok=True)
    combined_dir.mkdir(parents=True, exist_ok=True)

    scenario_files: Dict[int, Path] = {}
    for grid_size in GRIDS:
        scenario_path = scenario_dir / f"known_visit_grid{grid_size}_targets{args.num_targets}_trials{args.num_trials}.csv"
        generate_known_target_scenarios_for_grid(
            grid_size=grid_size,
            num_trials=args.num_trials,
            num_targets=args.num_targets,
            seed=args.scenario_seed,
            out_path=scenario_path,
        )
        scenario_files[grid_size] = scenario_path

    manifest_path = run_root / "condition_manifest.csv"
    header = [
        "condition_index",
        "condition_id",
        "grid_size",
        "grid_cells",
        "target_cells_per_robot",
        "robot_count",
        "actual_cells_per_robot",
        "density_error",
        "density_ratio",
        "num_targets",
        "comm_label",
        "comm_model",
        "comm_level",
        "algorithm_cli",
        "algorithm_name",
        "scenario_file",
        "num_trials",
        "out_dir",
        "seed",
        "robot_start_layout",
    ]

    rows: List[Dict[str, object]] = []
    condition_index = 0
    for grid_i, grid_size in enumerate(GRIDS):
        grid_cells = grid_size * grid_size
        for dens_i, target_density in enumerate(TARGET_DENSITIES):
            robot_count = ROBOT_COUNTS[grid_size][target_density]
            actual_density = grid_cells / float(robot_count)
            density_error = actual_density - float(target_density)
            density_ratio = actual_density / float(target_density)
            for comm_i, (comm_label, comm_model, comm_level) in enumerate(COMM_ENVS):
                sim_seed = args.sim_seed_base + grid_i * 10000 + dens_i * 1000 + comm_i * 100
                for alg_cli, alg_name in ALGORITHMS:
                    condition_id = f"g{grid_size}_d{target_density}_t{args.num_targets}_{comm_label}_{alg_cli}"
                    out_dir = raw_dir / f"grid{grid_size}" / f"density{target_density}" / comm_label / alg_cli
                    rows.append({
                        "condition_index": condition_index,
                        "condition_id": condition_id,
                        "grid_size": grid_size,
                        "grid_cells": grid_cells,
                        "target_cells_per_robot": target_density,
                        "robot_count": robot_count,
                        "actual_cells_per_robot": f"{actual_density:.6f}",
                        "density_error": f"{density_error:.6f}",
                        "density_ratio": f"{density_ratio:.6f}",
                        "num_targets": args.num_targets,
                        "comm_label": comm_label,
                        "comm_model": comm_model,
                        "comm_level": comm_level,
                        "algorithm_cli": alg_cli,
                        "algorithm_name": alg_name,
                        "scenario_file": str(scenario_files[grid_size]),
                        "num_trials": args.num_trials,
                        "out_dir": str(out_dir),
                        "seed": sim_seed,
                        "robot_start_layout": "edge_even",
                    })
                    condition_index += 1

    with manifest_path.open("w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=header)
        writer.writeheader()
        writer.writerows(rows)

    print(f"[OK] wrote scenarios: {scenario_dir}")
    print(f"[OK] wrote condition manifest: {manifest_path} ({len(rows)} conditions)")
    print(f"[INFO] expected system/trial rows after combine: {len(rows) * args.num_trials}")
    print(f"[INFO] targets per trial: {args.num_targets}")


if __name__ == "__main__":
    main()
