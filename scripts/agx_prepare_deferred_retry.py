#!/usr/bin/env python3
"""Build an exact, hash-audited retry config from unresolved attempt records."""

from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import os
from pathlib import Path
from typing import Any


ALGORITHMIC_FAILURE_TYPES = {
    "stagnation_horizon",
    "event_horizon",
    "event_queue_exhausted",
}


def _read_object(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"expected a JSON object: {path}")
    return value


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _write_json(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    data = json.dumps(value, indent=2, sort_keys=True).encode("utf-8") + b"\n"
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    temporary.write_bytes(data)
    os.replace(temporary, path)


def _classification(attempt_dir: Path, failure: dict[str, Any]) -> str:
    summary_path = attempt_dir / "trial_summary.json"
    if failure.get("return_code") != 0 or not summary_path.is_file():
        return "technical_failure"
    try:
        summary = _read_object(summary_path)
    except (OSError, ValueError, json.JSONDecodeError):
        return "technical_failure"
    if (
        summary.get("all_tasks_completed") is False
        and summary.get("causal_timing_enabled") is True
        and summary.get("mission_elapsed_time_s") is None
        and summary.get("algorithmic_failure_type") in ALGORITHMIC_FAILURE_TYPES
    ):
        return "misclassified_algorithmic_incomplete"
    return "technical_failure"


def prepare(
    base_config: Path,
    source_output_root: Path,
    campaign_id: str,
    output_root: str,
    config_out: Path,
    audit_out: Path,
) -> tuple[Path, Path]:
    config = _read_object(base_config)
    attempts_root = source_output_root / "attempts"
    completed_root = source_output_root / "completed"
    if not attempts_root.is_dir():
        raise ValueError(f"attempt root does not exist: {attempts_root}")

    failures_by_job: dict[str, list[tuple[Path, dict[str, Any]]]] = {}
    for path in sorted(attempts_root.glob("*/attempt_*/failure.json")):
        failure = _read_object(path)
        job_id = failure.get("job_id")
        if not isinstance(job_id, str) or not job_id:
            raise ValueError(f"failure record lacks job_id: {path}")
        failures_by_job.setdefault(job_id, []).append((path, failure))

    completed_ids = {
        path.parent.name
        for path in completed_root.glob("*/completion.json")
        if path.is_file()
    }
    selected_ids = sorted(set(failures_by_job) - completed_ids)
    if not selected_ids:
        raise ValueError("no unresolved failed jobs were found")

    records: list[dict[str, Any]] = []
    for job_id in selected_ids:
        for path, failure in failures_by_job[job_id]:
            attempt_dir = path.parent
            summary_path = attempt_dir / "trial_summary.json"
            records.append({
                "job_id": job_id,
                "failure_path": str(path.resolve()),
                "failure_sha256": _sha256(path),
                "summary_path": str(summary_path.resolve()) if summary_path.is_file() else None,
                "summary_sha256": _sha256(summary_path) if summary_path.is_file() else None,
                "classification": _classification(attempt_dir, failure),
                "message": failure.get("message"),
                "return_code": failure.get("return_code"),
            })

    campaign = config.get("campaign")
    if not isinstance(campaign, dict):
        raise ValueError("base config lacks campaign object")
    campaign["campaign_id"] = campaign_id
    campaign["output_root"] = output_root
    campaign["job_allowlist"] = selected_ids
    campaign["expected_selected_job_count"] = len(selected_ids)
    campaign["notes"] = (
        "Fresh-source deferred retry generated from immutable failure records; "
        "see the adjacent audit JSON for exact hashes and classifications."
    )

    generated_at = dt.datetime.now(dt.timezone.utc).isoformat().replace("+00:00", "Z")
    audit = {
        "schema_version": 1,
        "report_kind": "agx_deferred_retry_selection",
        "generated_at": generated_at,
        "base_config": str(base_config.resolve()),
        "base_config_sha256": _sha256(base_config),
        "source_output_root": str(source_output_root.resolve()),
        "campaign_id": campaign_id,
        "retry_output_root": output_root,
        "selected_job_count": len(selected_ids),
        "selected_job_ids": selected_ids,
        "failure_record_count": len(records),
        "classifications": {
            classification: sum(
                record["classification"] == classification for record in records
            )
            for classification in sorted({record["classification"] for record in records})
        },
        "records": records,
    }
    _write_json(config_out, config)
    _write_json(audit_out, audit)
    return config_out, audit_out


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-config", type=Path, required=True)
    parser.add_argument("--source-output-root", type=Path, required=True)
    parser.add_argument("--campaign-id", required=True)
    parser.add_argument("--output-root", required=True)
    parser.add_argument("--config-out", type=Path, required=True)
    parser.add_argument("--audit-out", type=Path, required=True)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    config, audit = prepare(
        args.base_config,
        args.source_output_root,
        args.campaign_id,
        args.output_root,
        args.config_out,
        args.audit_out,
    )
    print(f"retry config: {config.resolve()}")
    print(f"retry audit: {audit.resolve()}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
