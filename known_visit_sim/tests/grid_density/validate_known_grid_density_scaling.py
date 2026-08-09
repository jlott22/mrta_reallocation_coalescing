#!/usr/bin/env python3
"""
Quick smoke test for the known-visit grid-density scripts.
Runs two edge conditions, one trial each, under ideal communication.
"""
from __future__ import annotations

import csv
import subprocess
import sys
from pathlib import Path

CONDITIONS = (
    {"grid_size": 14, "robot_count": 1, "targets": [(1, 6), (2, 6), (3, 6), (4, 6), (5, 6), (6, 6), (7, 6), (8, 6), (9, 6), (10, 6)], "target_cpr": 196.0},
    {"grid_size": 48, "robot_count": 46, "targets": [(1, 0), (2, 0), (3, 0), (4, 0), (5, 0), (6, 0), (7, 0), (8, 0), (9, 0), (10, 0)], "target_cpr": 50.0},
)
ALGORITHMS = ("CBAA", "ACBBA", "PI", "HIPC", "DMCHBA", "DGA", "AuctionGreedy")


def write_scenario(path: Path, targets: list[tuple[int, int]]) -> None:
    header = ["trial_id"]
    for i in range(1, len(targets) + 1):
        header.extend([f"target{i}_x", f"target{i}_y"])
    row: list[object] = [0]
    for x, y in targets:
        row.extend([x, y])
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="") as f:
        writer = csv.writer(f)
        writer.writerow(header)
        writer.writerow(row)


def read_rows(path: Path) -> tuple[list[str], list[dict[str, str]]]:
    with path.open(newline="") as handle:
        reader = csv.DictReader(handle)
        return list(reader.fieldnames or []), list(reader)


def validate_run(out_dir: Path, robot_count: int, target_count: int) -> None:
    expected = {
        "system_performance.csv": 1,
        "trial_summary.csv": 1,
        "robot_performance.csv": robot_count,
        "target_performance.csv": target_count,
    }
    for filename, expected_rows in expected.items():
        path = out_dir / filename
        if not path.is_file():
            raise AssertionError(f"missing output: {path}")
        _fields, rows = read_rows(path)
        if len(rows) != expected_rows:
            raise AssertionError(f"{path} has {len(rows)} rows; expected {expected_rows}")


def main() -> None:
    package_dir = Path(__file__).resolve().parents[2]
    repo_root = package_dir.parent
    output_root = repo_root / "results" / "known_target_visit_grid_density_validation"
    output_root.mkdir(parents=True, exist_ok=True)

    for condition in CONDITIONS:
        grid_size = condition["grid_size"]
        robot_count = condition["robot_count"]
        condition_id = f"known_g{grid_size}_r{robot_count}"
        scenario_path = output_root / f"scenario_{condition_id}.csv"
        write_scenario(scenario_path, list(condition["targets"]))
        actual_cpr = grid_size * grid_size / robot_count

        for algorithm in ALGORITHMS:
            out_dir = output_root / condition_id / algorithm.lower()
            cmd = [
                sys.executable,
                str(package_dir / "tests" / "grid_density" / "run_known_grid_density_worker.py"),
                "--repo-root", str(repo_root),
                "--manifest", str(output_root / f"manifest_{condition_id}_{algorithm}.csv"),
                "--worker-index", "0",
                "--num-workers", "1",
                "--force",
            ]
            manifest_path = output_root / f"manifest_{condition_id}_{algorithm}.csv"
            fields = [
                "condition_index", "condition_id", "grid_size", "grid_cells", "target_cells_per_robot",
                "robot_count", "actual_cells_per_robot", "density_error", "density_ratio", "num_targets",
                "comm_label", "comm_model", "comm_level", "algorithm_cli", "algorithm_name", "scenario_file",
                "num_trials", "out_dir", "seed", "robot_start_layout",
            ]
            row = {
                "condition_index": 0,
                "condition_id": f"{condition_id}_{algorithm.lower()}",
                "grid_size": grid_size,
                "grid_cells": grid_size * grid_size,
                "target_cells_per_robot": condition["target_cpr"],
                "robot_count": robot_count,
                "actual_cells_per_robot": actual_cpr,
                "density_error": actual_cpr - condition["target_cpr"],
                "density_ratio": actual_cpr / condition["target_cpr"],
                "num_targets": len(condition["targets"]),
                "comm_label": "ideal",
                "comm_model": "ideal",
                "comm_level": "1.0",
                "algorithm_cli": algorithm.lower(),
                "algorithm_name": algorithm,
                "scenario_file": str(scenario_path.resolve()),
                "num_trials": 1,
                "out_dir": str(out_dir.resolve()),
                "seed": 910000,
                "robot_start_layout": "edge_even",
            }
            with manifest_path.open("w", newline="") as f:
                writer = csv.DictWriter(f, fieldnames=fields)
                writer.writeheader()
                writer.writerow(row)
            subprocess.run(cmd, cwd=repo_root, check=True)
            validate_run(out_dir, robot_count, len(condition["targets"]))
            print(f"validated {condition_id}/{algorithm}")


if __name__ == "__main__":
    main()
