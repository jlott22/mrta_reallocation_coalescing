#!/usr/bin/env python3
"""Combine known-visit grid-density sensitivity raw outputs into one combined directory."""
from __future__ import annotations

import argparse
import csv
from pathlib import Path
from typing import Dict, Iterable, List, Tuple

CSV_TYPES = [
    "system_performance.csv",
    "trial_summary.csv",
    "robot_performance.csv",
    "target_performance.csv",
]

BACKFILL_COLS = [
    "condition_id",
    "grid_size",
    "grid_cells",
    "target_cells_per_robot",
    "robot_count",
    "actual_cells_per_robot",
    "num_targets",
    "comm_model",
    "comm_level",
    "algorithm_name",
    "scenario_file",
]


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run-root", default="/home/jlott/dcta_benchmark_sim/results/sensitivity_known_target_visit_grid_density_50")
    return parser.parse_args()


def read_manifest(path: Path) -> List[Dict[str, str]]:
    with path.open(newline="") as f:
        return list(csv.DictReader(f))


def read_csv_rows(path: Path) -> Tuple[List[str], List[Dict[str, str]]]:
    with path.open(newline="") as f:
        reader = csv.DictReader(f)
        rows = list(reader)
        return list(reader.fieldnames or []), rows


def write_rows(path: Path, fieldnames: List[str], rows: List[Dict[str, str]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)


def add_fieldnames(existing: List[str], new_cols: Iterable[str]) -> List[str]:
    out = list(existing)
    for col in new_cols:
        if col not in out:
            out.append(col)
    return out


def main() -> None:
    args = parse_args()
    run_root = Path(args.run_root).resolve()
    manifest_path = run_root / "condition_manifest.csv"
    combined_dir = run_root / "combined"

    manifest_rows = read_manifest(manifest_path)
    print(f"[INFO] Combining {len(manifest_rows)} condition rows from {manifest_path}")

    source_manifest: List[Dict[str, str]] = []

    for csv_name in CSV_TYPES:
        all_rows: List[Dict[str, str]] = []
        fieldnames: List[str] = []
        missing = 0

        for cond in manifest_rows:
            in_path = Path(cond["out_dir"]) / csv_name
            if not in_path.exists() or in_path.stat().st_size == 0:
                missing += 1
                source_manifest.append({
                    "condition_id": cond["condition_id"],
                    "csv_type": csv_name,
                    "path": str(in_path),
                    "rows": "0",
                    "status": "missing",
                })
                continue

            input_fields, rows = read_csv_rows(in_path)
            fieldnames = add_fieldnames(fieldnames, input_fields)
            fieldnames = add_fieldnames(fieldnames, BACKFILL_COLS)

            for row in rows:
                for col in BACKFILL_COLS:
                    if not row.get(col, ""):
                        row[col] = cond.get(col, "")
                if not row.get("algorithm", "") and cond.get("algorithm_name"):
                    row["algorithm"] = cond["algorithm_name"]
                    fieldnames = add_fieldnames(fieldnames, ["algorithm"])
                all_rows.append(row)

            source_manifest.append({
                "condition_id": cond["condition_id"],
                "csv_type": csv_name,
                "path": str(in_path),
                "rows": str(len(rows)),
                "status": "ok",
            })

        out_path = combined_dir / csv_name
        if all_rows:
            write_rows(out_path, fieldnames, all_rows)
        else:
            write_rows(out_path, BACKFILL_COLS, [])
        print(f"[OK] wrote {out_path} rows={len(all_rows)} missing_inputs={missing}")

    source_fields = ["condition_id", "csv_type", "path", "rows", "status"]
    write_rows(combined_dir / "source_manifest.csv", source_fields, source_manifest)
    print(f"[OK] wrote {combined_dir / 'source_manifest.csv'}")


if __name__ == "__main__":
    main()
