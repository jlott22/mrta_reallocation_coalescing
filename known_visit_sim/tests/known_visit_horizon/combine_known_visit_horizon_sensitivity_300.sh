#!/usr/bin/env bash
set -u
set -o pipefail

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="${DCTA_REPO_ROOT:-$(cd -- "$SCRIPT_DIR/../../.." && pwd)}"
if [[ ! -d "$REPO_ROOT/known_visit_sim" ]]; then
  echo "ERROR: known_visit_sim package not found under $REPO_ROOT" >&2
  exit 2
fi
cd "$REPO_ROOT" || exit 1
PYTHON_BIN="${PYTHON_BIN:-python3}"

"$PYTHON_BIN" - <<'PY'
import csv
from pathlib import Path

ROOT = Path("results/sensitivity_known_target_visit_horizon_300")
FILENAMES = [
    "system_performance.csv",
    "trial_summary.csv",
    "robot_performance.csv",
    "target_performance.csv",
]
META_COLS = [
    "sensitivity_suite",
    "sensitivity_parameter",
    "sensitivity_value",
    "sensitivity_label",
    "source_comm_folder",
    "source_condition_folder",
    "source_out_dir",
]
TARGET_ROWS_PER_PART = 120_000


def infer_metadata(csv_file: Path) -> dict:
    rel = csv_file.relative_to(ROOT)
    parts = rel.parts
    if len(parts) < 5 or parts[0] != "raw":
        raise ValueError(f"Unexpected path: {csv_file}")
    label = parts[1]
    if not label.startswith("h"):
        raise ValueError(f"Expected horizon label h#, got {label}: {csv_file}")
    return {
        "sensitivity_suite": "known_visit_horizon",
        "sensitivity_parameter": "commitment_horizon",
        "sensitivity_value": label[1:],
        "sensitivity_label": label,
        "source_comm_folder": parts[2],
        "source_condition_folder": parts[3],
        "source_out_dir": str(csv_file.parent),
    }


def count_data_rows(path: Path) -> int:
    if not path.exists():
        return 0
    with path.open("r", newline="", encoding="utf-8") as f:
        return max(sum(1 for _ in f) - 1, 0)


def find_raw(filename: str):
    raw = ROOT / "raw"
    return sorted(p for p in raw.rglob(filename) if p.is_file()) if raw.exists() else []


def combine_one(filename: str):
    files = find_raw(filename)
    out_dir = ROOT / "combined"
    out_dir.mkdir(parents=True, exist_ok=True)
    if not files:
        print(f"[SKIP] no {filename}")
        return
    rows_written = 0
    fieldnames = []
    seen = set()
    rows = []
    for f in files:
        meta = infer_metadata(f)
        with f.open(newline="", encoding="utf-8") as handle:
            reader = csv.DictReader(handle)
            for name in reader.fieldnames or []:
                if name not in seen:
                    seen.add(name)
                    fieldnames.append(name)
            for row in reader:
                row.update(meta)
                rows.append(row)
                rows_written += 1
    for name in META_COLS:
        if name not in seen:
            seen.add(name)
            fieldnames.append(name)
    stem = Path(filename).stem
    for stale in out_dir.glob(f"{stem}_part*.csv"):
        stale.unlink()
    if filename == "target_performance.csv":
        (out_dir / filename).unlink(missing_ok=True)
        outputs = []
        for index, start in enumerate(range(0, len(rows), TARGET_ROWS_PER_PART), start=1):
            outfile = out_dir / f"{stem}_part{index}.csv"
            with outfile.open("w", newline="", encoding="utf-8") as f:
                writer = csv.DictWriter(f, fieldnames=fieldnames, extrasaction="ignore")
                writer.writeheader()
                writer.writerows(rows[start:start + TARGET_ROWS_PER_PART])
            outputs.append(outfile)
        print(f"[OK] wrote {len(outputs)} target metric parts rows={rows_written}")
    else:
        outfile = out_dir / filename
        with outfile.open("w", newline="", encoding="utf-8") as f:
            writer = csv.DictWriter(f, fieldnames=fieldnames, extrasaction="ignore")
            writer.writeheader()
            writer.writerows(rows)
        print(f"[OK] wrote {outfile} rows={rows_written}")


def write_condition_manifest():
    out = ROOT / "combined" / "condition_manifest.csv"
    out.parent.mkdir(parents=True, exist_ok=True)
    files = find_raw("system_performance.csv")
    fields = META_COLS + ["system_rows", "trial_rows", "robot_rows", "target_rows", "complete_marker"]
    with out.open("w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fields)
        writer.writeheader()
        for system_file in files:
            meta = infer_metadata(system_file)
            d = Path(meta["source_out_dir"])
            row = dict(meta)
            row["system_rows"] = count_data_rows(d / "system_performance.csv")
            row["trial_rows"] = count_data_rows(d / "trial_summary.csv")
            row["robot_rows"] = count_data_rows(d / "robot_performance.csv")
            row["target_rows"] = count_data_rows(d / "target_performance.csv")
            row["complete_marker"] = "yes" if (d / "_COMPLETE.txt").exists() else "no"
            writer.writerow(row)
    print(f"[OK] wrote {out}")

for filename in FILENAMES:
    combine_one(filename)
write_condition_manifest()
print("[DONE] combined known-visit horizon sensitivity")
PY
