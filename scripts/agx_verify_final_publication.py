#!/usr/bin/env python3
"""Fail-closed validation for the compact final publication bundle."""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import math
from pathlib import Path
from typing import Any


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _rows(path: Path) -> list[dict[str, str]]:
    with path.open(newline="", encoding="utf-8-sig") as handle:
        return list(csv.DictReader(handle))


def _truth(value: str) -> bool:
    return value.strip().lower() in {"true", "1", "yes"}


def verify(root: Path) -> dict[str, Any]:
    manifest_path = root / "publication_manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    if manifest.get("report_kind") != "mrta_final_publication_bundle":
        raise ValueError("publication manifest kind mismatch")
    for relative, expected in manifest["files"].items():
        path = root / relative
        if not path.is_file():
            raise FileNotFoundError(f"manifest file is missing: {relative}")
        if path.stat().st_size != expected["bytes"]:
            raise ValueError(f"manifest byte count mismatch: {relative}")
        if _sha256(path) != expected["sha256"]:
            raise ValueError(f"manifest hash mismatch: {relative}")

    primary = _rows(root / "data/primary_trial_level.csv")
    causal = [row for row in primary if row["dataset"] == "causal"]
    zero = [row for row in primary if row["dataset"] == "zero_compute"]
    paired = _rows(root / "data/causal_zero_paired_trial_level.csv")
    policy = _rows(root / "data/policy_vs_eager_paired_trial_level.csv")
    hardware = _rows(root / "data/hardware_trial_level.csv")
    verification = _rows(root / "data/verification_trial_level.csv")
    noncompletions = _rows(root / "data/legitimate_noncompletions.csv")
    conditions = _rows(root / "data/primary_condition_summary.csv")
    paired_conditions = _rows(root / "data/paired_condition_summary.csv")
    task_conditions = _rows(root / "data/task_condition_summary.csv")
    audit = _rows(root / "data/execution_audit.csv")

    expected_counts = {
        "primary": (len(primary), 6000),
        "causal": (len(causal), 3000),
        "zero_compute": (len(zero), 3000),
        "paired": (len(paired), 3000),
        "policy_pairs": (len(policy), 4800),
        "hardware": (len(hardware), 104),
        "verification": (len(verification), 144),
        "noncompletions": (len(noncompletions), 33),
        "condition_rows": (len(conditions), 120),
        "paired_condition_rows": (len(paired_conditions), 60),
        "task_condition_rows": (len(task_conditions), 120),
    }
    for label, (actual, expected) in expected_counts.items():
        if actual != expected:
            raise ValueError(f"{label} count mismatch: {actual} != {expected}")

    def key(row: dict[str, str]) -> tuple[str, str, str, str]:
        return row["algorithm"], row["arrival_load"], row["policy_id"], row["trace_id"]

    causal_keys, zero_keys = {key(row) for row in causal}, {key(row) for row in zero}
    if len(causal_keys) != 3000 or causal_keys != zero_keys:
        raise ValueError("primary pairing keys are incomplete or duplicated")
    if sum(_truth(row["both_completed"]) for row in paired) != 2967:
        raise ValueError("both-completed pair count mismatch")
    if sum(_truth(row["parity_passed"]) for row in hardware) != 104:
        raise ValueError("hardware parity coverage mismatch")
    if any(row["algorithmic_failure_type"] != "stagnation_horizon" for row in noncompletions):
        raise ValueError("unexpected primary noncompletion class")
    if sum(int(row["technical_attempt_failure_n"]) for row in audit) != 3:
        raise ValueError("technical attempt audit count mismatch")
    if sum(int(row["unresolved_technical_failure_n"]) for row in audit) != 0:
        raise ValueError("publication has unresolved technical failures")

    for path in sorted((root / "data").glob("*.csv")):
        text = path.read_text(encoding="utf-8")
        if "/home/" in text or "artifact_dir" in text:
            raise ValueError(f"machine-local path leaked into {path.name}")
        for token in text.replace("\r", "").replace("\n", ",").split(","):
            normalized = token.strip().lower()
            if normalized in {"nan", "inf", "+inf", "-inf", "infinity"}:
                raise ValueError(f"non-finite numeric token in {path.name}")
    for path in sorted((root / "figures").glob("*.pdf")):
        if path.stat().st_size < 1000 or path.read_bytes()[:4] != b"%PDF":
            raise ValueError(f"invalid publication figure: {path.name}")

    return {
        "validated_file_count": len(manifest["files"]),
        "primary_trial_count": len(primary),
        "paired_trial_count": len(paired),
        "both_completed_pair_count": 2967,
        "hardware_trial_count": len(hardware),
        "verification_trial_count": len(verification),
        "legitimate_noncompletion_count": len(noncompletions),
        "technical_attempt_failures_recovered": 3,
        "unresolved_technical_failures": 0,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("root", type=Path)
    args = parser.parse_args()
    print(json.dumps(verify(args.root.resolve()), indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
