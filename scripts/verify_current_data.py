#!/usr/bin/env python3
"""Verify canonical corrected data integrity and coverage without analysis."""

from __future__ import annotations

import csv
import gzip
import hashlib
import json
from collections import Counter
from pathlib import Path
from typing import Any, Iterable


ROOT = Path(__file__).resolve().parents[1]
AGX = ROOT / "corrected_agx_v7_results"
HARDWARE = ROOT / "corrected_hardware_v9_v10_progress"


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _object(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"expected JSON object: {path}")
    return value


def _verify_manifest(bundle: Path, expected_id: str) -> dict[str, Any]:
    manifest = _object(bundle / "export_manifest.json")
    if manifest.get("bundle_id") != expected_id:
        raise ValueError(f"unexpected bundle identity: {bundle}")
    files = manifest.get("files")
    if not isinstance(files, dict) or not files:
        raise ValueError(f"bundle lacks file inventory: {bundle}")
    for relative, metadata in files.items():
        if not isinstance(relative, str) or not isinstance(metadata, dict):
            raise ValueError(f"invalid file inventory entry: {bundle}")
        path = (bundle / relative).resolve()
        if bundle.resolve() not in path.parents:
            raise ValueError(f"bundle path escapes root: {relative}")
        if not path.is_file():
            raise ValueError(f"missing bundle file: {path}")
        if path.stat().st_size != metadata.get("bytes"):
            raise ValueError(f"file-size mismatch: {path}")
        if _sha256(path) != metadata.get("sha256"):
            raise ValueError(f"SHA-256 mismatch: {path}")
    return manifest


def _csv_rows(path: Path) -> list[dict[str, str]]:
    if path.suffix == ".gz":
        with gzip.open(path, "rt", encoding="utf-8", newline="") as handle:
            return list(csv.DictReader(handle))
    with path.open("r", encoding="utf-8", newline="") as handle:
        return list(csv.DictReader(handle))


def _jsonl_rows(path: Path) -> Iterable[dict[str, Any]]:
    with gzip.open(path, "rt", encoding="utf-8") as handle:
        for line in handle:
            value = json.loads(line)
            if not isinstance(value, dict):
                raise ValueError(f"non-object JSONL row: {path}")
            yield value


def _verify_agx() -> dict[str, int]:
    manifest = _verify_manifest(AGX, "corrected_agx_v7_results")
    expected_campaigns = {
        "corrected_round1_causal_v7",
        "corrected_round1_zero_v7",
        "corrected_round2_causal_v7",
        "corrected_round2_zero_v7",
    }
    if any((
        manifest.get("trial_count") != 6000,
        manifest.get("paired_causal_zero_count") != 3000,
        manifest.get("completion_record_count") != 6000,
    )):
        raise ValueError("AGX manifest counts changed")
    rows = _csv_rows(AGX / "trial_level.csv.gz")
    if len(rows) != 6000:
        raise ValueError("AGX trial table must contain 6,000 rows")
    campaigns = {row["campaign_id"] for row in rows}
    if campaigns != expected_campaigns:
        raise ValueError("AGX trial table contains an unexpected campaign")
    providers = Counter(row["timing_provider"] for row in rows)
    if providers != Counter({
        "HostMeasuredTimingProvider": 3000,
        "ZeroComputeTimingProvider": 3000,
    }):
        raise ValueError("AGX timing-provider coverage changed")
    causal = [row for row in rows if row["timing_provider"] == "HostMeasuredTimingProvider"]
    zero = [row for row in rows if row["timing_provider"] == "ZeroComputeTimingProvider"]
    incomplete = [row for row in causal if row["algorithmic_status"] == "incomplete"]
    if len(causal) != 3000 or len(zero) != 3000 or len(incomplete) != 60:
        raise ValueError("AGX arm or noncompletion counts changed")
    if any(row["algorithmic_status"] != "completed" for row in zero):
        raise ValueError("zero-compute arm contains an unexpected noncompletion")
    pairs = _csv_rows(AGX / "causal_zero_pairs.csv.gz")
    if len(pairs) != 3000:
        raise ValueError("AGX causal-zero pair count changed")
    return {"trials": len(rows), "pairs": len(pairs), "causal_noncompletions": len(incomplete)}


def _verify_hardware() -> dict[str, int]:
    manifest = _verify_manifest(HARDWARE, "corrected_hardware_v9_v10_progress")
    if any((
        manifest.get("planned_trials") != 96,
        manifest.get("completed_trials") != 82,
        manifest.get("remaining_terminal_failed_trials") != 14,
        manifest.get("attempt_failure_count") != 34,
    )):
        raise ValueError("hardware manifest counts changed")
    rows = _csv_rows(HARDWARE / "trial_level.csv.gz")
    completed_ids = {row["job_id"] for row in rows}
    if len(rows) != 82 or len(completed_ids) != 82:
        raise ValueError("hardware success rows are not 82 unique jobs")
    allowed_campaigns = {
        "corrected_hardware_core_96_v9",
        "corrected_hardware_core_96_v10_continuation",
    }
    for row in rows:
        if row["campaign_id"] not in allowed_campaigns:
            raise ValueError("hardware table contains an unexpected campaign")
        if row["timing_provider"] != "_OwnedPhysicalSession":
            raise ValueError("hardware table contains a nonphysical timing provider")
        if any(row[field] != "true" for field in (
            "hardware_validated", "parity_passed", "all_tasks_completed"
        )):
            raise ValueError(f"hardware success lacks validation: {row['job_id']}")
    terminal = _csv_rows(HARDWARE / "terminal_failures.csv")
    failure_ids = {row["job_id"] for row in terminal}
    if len(terminal) != 14 or len(failure_ids) != 14:
        raise ValueError("terminal failures are not 14 unique jobs")
    if completed_ids & failure_ids:
        raise ValueError("hardware successes overlap terminal failures")
    expected_ids = {
        f"{algorithm}__{load}__trace_{trace:04d}__{policy}"
        for algorithm in ("CBAA", "ACBBA", "PI", "HIPC")
        for load in ("low", "medium", "high")
        for trace in range(4)
        for policy in ("eager_b1", "count_b4")
    }
    if completed_ids | failure_ids != expected_ids:
        raise ValueError("successes and failures do not partition the 96-job design")
    retry_config = _object(ROOT / "configs/corrected/hardware_optional_retry_14.json")
    retry_spec = retry_config.get("campaign", {}).get("job_allowlist", {})
    if not isinstance(retry_spec, dict):
        raise ValueError("optional retry config lacks a job allowlist")
    allowlist_path = ROOT / str(retry_spec.get("path", ""))
    predecessor_path = ROOT / str(retry_spec.get("predecessor_export_manifest", ""))
    if any((
        allowlist_path.resolve() != (HARDWARE / "terminal_failures.csv").resolve(),
        predecessor_path.resolve() != (HARDWARE / "export_manifest.json").resolve(),
        _sha256(allowlist_path) != retry_spec.get("sha256"),
        _sha256(predecessor_path)
        != retry_spec.get("predecessor_export_manifest_sha256"),
        retry_spec.get("expected_planned_jobs") != 96,
        retry_spec.get("expected_completed_jobs") != 82,
        retry_spec.get("expected_retry_jobs") != 14,
    )):
        raise ValueError("optional retry config is not bound to the current checkpoint")
    algorithm_failures = Counter(row["algorithm"] for row in terminal)
    load_failures = Counter(row["arrival_load"] for row in terminal)
    if algorithm_failures != Counter({"ACBBA": 11, "PI": 2, "CBAA": 1}):
        raise ValueError("hardware failure distribution by algorithm changed")
    if load_failures != Counter({"high": 9, "medium": 3, "low": 2}):
        raise ValueError("hardware failure distribution by load changed")
    attempts = _csv_rows(HARDWARE / "attempt_failures.csv")
    if len(attempts) != 34:
        raise ValueError("hardware attempt-failure count changed")
    attempt_categories = Counter(row["category"] for row in attempts)
    if attempt_categories != Counter({
        "parity": 24,
        "result_chunk_sequence": 6,
        "memory": 3,
        "timeout_or_no_response": 1,
    }):
        raise ValueError("hardware attempt-failure categories changed")
    completion_ids = {row["job_id"] for row in _jsonl_rows(HARDWARE / "completion_records.jsonl.gz")}
    if completion_ids != completed_ids:
        raise ValueError("hardware completion records differ from trial rows")
    return {"planned": 96, "successes": len(rows), "terminal_failures": len(terminal), "failed_attempts": len(attempts)}


def main() -> int:
    report = {
        "agx": _verify_agx(),
        "hardware": _verify_hardware(),
        "status": "PASS",
    }
    print(json.dumps(report, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
