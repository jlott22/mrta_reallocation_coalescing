#!/usr/bin/env python3
"""Verify expected row counts and condition coverage for the known-visit grid-density sensitivity study."""
from __future__ import annotations

import argparse
import csv
from collections import Counter, defaultdict
from pathlib import Path

EXPECTED_GRIDS = 5
EXPECTED_DENSITIES = 7
EXPECTED_COMMS = 4
EXPECTED_ALGORITHMS = 7
EXPECTED_CONDITIONS = EXPECTED_GRIDS * EXPECTED_DENSITIES * EXPECTED_COMMS * EXPECTED_ALGORITHMS


def parse_args():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--run-root", default="/home/jlott/dcta_benchmark_sim/results/sensitivity_known_target_visit_grid_density_50")
    return p.parse_args()


def read_rows(path: Path):
    with path.open(newline="") as f:
        return list(csv.DictReader(f))


def main():
    args = parse_args()
    run_root = Path(args.run_root).resolve()
    manifest = read_rows(run_root / "condition_manifest.csv")
    print(f"[CHECK] condition_manifest rows: {len(manifest)} expected {EXPECTED_CONDITIONS}")
    expected_system_rows = sum(int(row["num_trials"]) for row in manifest)
    expected_target_rows = sum(int(row["num_trials"]) * int(row.get("num_targets", "10")) for row in manifest)
    expected_robot_rows = sum(int(row["num_trials"]) * int(row["robot_count"]) for row in manifest)

    sys_path = run_root / "combined" / "system_performance.csv"
    trial_path = run_root / "combined" / "trial_summary.csv"
    robot_path = run_root / "combined" / "robot_performance.csv"
    target_path = run_root / "combined" / "target_performance.csv"

    sys_rows = read_rows(sys_path) if sys_path.exists() else []
    trial_rows = read_rows(trial_path) if trial_path.exists() else []
    robot_rows = read_rows(robot_path) if robot_path.exists() else []
    target_rows = read_rows(target_path) if target_path.exists() else []

    print(f"[CHECK] system_performance rows: {len(sys_rows)} expected {expected_system_rows}")
    print(f"[CHECK] trial_summary rows:      {len(trial_rows)} expected {expected_system_rows}")
    print(f"[CHECK] robot_performance rows: {len(robot_rows)} expected {expected_robot_rows}")
    print(f"[CHECK] target_performance rows:{len(target_rows)} expected {expected_target_rows}")

    manifest_by_id = {row["condition_id"]: row for row in manifest}
    system_counts = Counter(row.get("condition_id", "") for row in sys_rows)
    trial_counts = Counter(row.get("condition_id", "") for row in trial_rows)
    robot_counts = Counter(row.get("condition_id", "") for row in robot_rows)
    target_counts = Counter(row.get("condition_id", "") for row in target_rows)

    missing = [cid for cid in manifest_by_id if system_counts.get(cid, 0) == 0]
    bad = []
    for cid, row in manifest_by_id.items():
        trials = int(row["num_trials"])
        expected_robots = trials * int(row["robot_count"])
        expected_targets = trials * int(row.get("num_targets", "10"))
        if (
            system_counts.get(cid, 0) != trials
            or trial_counts.get(cid, 0) != trials
            or robot_counts.get(cid, 0) != expected_robots
            or target_counts.get(cid, 0) != expected_targets
        ):
            bad.append((cid, system_counts.get(cid, 0), trial_counts.get(cid, 0), robot_counts.get(cid, 0), target_counts.get(cid, 0)))

    expected_trial_ids = {
        cid: {str(index) for index in range(int(row["num_trials"]))}
        for cid, row in manifest_by_id.items()
    }
    ids_by_condition = defaultdict(set)
    for row in sys_rows:
        ids_by_condition[row.get("condition_id", "")].add(str(row.get("trial_id", "")))
    bad_trial_ids = [cid for cid, expected in expected_trial_ids.items() if ids_by_condition.get(cid, set()) != expected]

    print(f"[CHECK] conditions with system rows: {len(system_counts)}")
    print(f"[CHECK] missing conditions: {len(missing)}")
    print(f"[CHECK] conditions with incorrect row counts: {len(bad)}")
    print(f"[CHECK] conditions with incorrect system trial IDs: {len(bad_trial_ids)}")
    if missing[:10]:
        print("[WARN] first missing:", missing[:10])
    if bad[:10]:
        print("[WARN] first bad counts (condition, system, trial, robot, target):", bad[:10])
    if bad_trial_ids[:10]:
        print("[WARN] first bad trial-ID conditions:", bad_trial_ids[:10])

    required_cols = [
        "trial_id", "grid_size", "target_cells_per_robot", "robot_count",
        "actual_cells_per_robot", "num_targets", "comm_model", "comm_level", "condition_id",
    ]
    missing_cols = required_cols
    if sys_rows:
        cols = set(sys_rows[0].keys())
        missing_cols = [c for c in required_cols if c not in cols]
        print(f"[CHECK] missing required system columns: {missing_cols}")

    verification_ok = (
        len(manifest) == EXPECTED_CONDITIONS
        and len(sys_rows) == expected_system_rows
        and len(trial_rows) == expected_system_rows
        and len(robot_rows) == expected_robot_rows
        and len(target_rows) == expected_target_rows
        and not missing
        and not bad
        and not bad_trial_ids
        and not missing_cols
    )
    if verification_ok:
        print("[OK] verification passed")
    else:
        print("[WARN] verification found mismatches; inspect logs/source_manifest")
        raise SystemExit(1)


if __name__ == "__main__":
    main()
