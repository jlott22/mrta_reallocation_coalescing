#!/usr/bin/env python3
"""Combine and validate all known-visit core-500 metric CSV files."""

from __future__ import annotations

import csv
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[3]
ROOT = REPO_ROOT / "results" / "known_target_visit_core_500"
RAW = ROOT / "raw"
COMBINED = ROOT / "combined"
METRIC_FILES = (
    "system_performance.csv",
    "trial_summary.csv",
    "robot_performance.csv",
    "target_performance.csv",
)
META_FIELDS = ("algorithm", "communication_condition", "source_out_dir")
EXPECTED_ROWS = {
    "system_performance.csv": 75_000,
    "trial_summary.csv": 75_000,
    "robot_performance.csv": 300_000,
    "target_performance.csv": 750_000,
}
TARGET_ROWS_PER_PART = 250_000


def source_files(filename: str) -> list[Path]:
    return sorted(RAW.glob(f"*/*/{filename}"))


def metadata(path: Path) -> dict[str, str]:
    relative = path.relative_to(RAW)
    return {
        "algorithm": relative.parts[0].upper(),
        "communication_condition": relative.parts[1],
        "source_out_dir": str(path.parent.relative_to(REPO_ROOT)),
    }


def combine(filename: str) -> int:
    files = source_files(filename)
    if len(files) != 150:
        raise RuntimeError(f"{filename}: expected 150 source files, found {len(files)}")

    fieldnames: list[str] = []
    seen: set[str] = set()
    for path in files:
        with path.open(newline="", encoding="utf-8") as handle:
            names = csv.DictReader(handle).fieldnames or []
        for name in (*names, *META_FIELDS):
            if name not in seen:
                seen.add(name)
                fieldnames.append(name)

    COMBINED.mkdir(parents=True, exist_ok=True)
    rows_written = 0
    part_number = 0
    rows_in_part = 0
    destination = None
    writer = None

    for stale in COMBINED.glob(f"{Path(filename).stem}_part*.csv"):
        stale.unlink()
    if filename == "target_performance.csv":
        (COMBINED / filename).unlink(missing_ok=True)

    try:
        for path in files:
            meta = metadata(path)
            with path.open(newline="", encoding="utf-8") as source:
                for row in csv.DictReader(source):
                    if writer is None or (
                        filename == "target_performance.csv"
                        and rows_in_part == TARGET_ROWS_PER_PART
                    ):
                        if destination is not None:
                            destination.close()
                        part_number += 1
                        rows_in_part = 0
                        output = (
                            COMBINED / f"target_performance_part{part_number}.csv"
                            if filename == "target_performance.csv"
                            else COMBINED / filename
                        )
                        destination = output.open("w", newline="", encoding="utf-8")
                        writer = csv.DictWriter(
                            destination, fieldnames=fieldnames, extrasaction="ignore"
                        )
                        writer.writeheader()
                    row.update(meta)
                    writer.writerow(row)
                    rows_written += 1
                    rows_in_part += 1
    finally:
        if destination is not None:
            destination.close()

    expected = EXPECTED_ROWS[filename]
    if rows_written != expected:
        raise RuntimeError(f"{filename}: expected {expected:,} rows, wrote {rows_written:,}")
    outputs = (
        f"{part_number} target_performance parts"
        if filename == "target_performance.csv"
        else str((COMBINED / filename).relative_to(REPO_ROOT))
    )
    print(f"[OK] {outputs}: {rows_written:,} rows from {len(files)} conditions")
    return rows_written


def write_condition_manifest() -> None:
    output = COMBINED / "condition_metrics_manifest.csv"
    fields = [
        "algorithm", "communication_condition", "source_out_dir",
        "system_rows", "trial_rows", "robot_rows", "target_rows", "complete",
    ]
    with output.open("w", newline="", encoding="utf-8") as destination:
        writer = csv.DictWriter(destination, fieldnames=fields)
        writer.writeheader()
        for directory in sorted(path for path in RAW.glob("*/*") if path.is_dir()):
            row = {
                "algorithm": directory.parent.name.upper(),
                "communication_condition": directory.name,
                "source_out_dir": str(directory.relative_to(REPO_ROOT)),
                "complete": "yes" if (directory / "_COMPLETE.txt").exists() else "no",
            }
            for column, filename in (
                ("system_rows", "system_performance.csv"),
                ("trial_rows", "trial_summary.csv"),
                ("robot_rows", "robot_performance.csv"),
                ("target_rows", "target_performance.csv"),
            ):
                with (directory / filename).open(newline="", encoding="utf-8") as handle:
                    row[column] = sum(1 for _ in csv.DictReader(handle))
            writer.writerow(row)
    print(f"[OK] {output.relative_to(REPO_ROOT)}: 150 conditions")


def main() -> None:
    for filename in METRIC_FILES:
        combine(filename)
    write_condition_manifest()
    print("[DONE] combined and validated all known-visit core-500 metrics")


if __name__ == "__main__":
    main()
