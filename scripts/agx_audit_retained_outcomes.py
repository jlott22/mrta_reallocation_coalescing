#!/usr/bin/env python3
"""Reclassify retained, technically successful algorithmic noncompletions.

This is deliberately an audit operation, not a campaign resume.  It never
launches a trial, deletes a failure record, or moves an attempt into the
immutable completed tree.  The report binds each post-hoc classification to
the exact retained output hashes and records the historical validator failure
that caused the outcome to be omitted originally.
"""

from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import os
import tempfile
from pathlib import Path
from typing import Any

from study.campaign import CampaignOrchestrator, validate_job_outputs


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _read_object(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"expected a JSON object: {path}")
    return value


def _atomic_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    temporary.write_text(
        json.dumps(value, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    os.replace(temporary, path)


def _legacy_status_normalization(summary: dict[str, Any]) -> dict[str, Any] | None:
    """Map the pre-classification schema aliases without changing raw evidence."""

    if not (
        summary.get("schema_version") == 2
        and summary.get("causal_timing_enabled") is True
        and summary.get("all_tasks_completed") is False
        and summary.get("mission_elapsed_time_s") is None
        and summary.get("algorithmic_status") == "incomplete"
        and summary.get("algorithmic_failure_type")
        in {"stagnation_horizon", "event_horizon", "event_queue_exhausted"}
        and "technical_status" not in summary
        and summary.get("trial_status") == "completed"
        and "failure_type" not in summary
    ):
        return None
    return {
        "technical_status": "completed",
        "trial_status": "algorithmic_incomplete",
        "failure_type": summary["algorithmic_failure_type"],
    }


def _validate_retained(
    orchestrator: CampaignOrchestrator,
    job: Any,
    attempt: Path,
    summary: dict[str, Any],
) -> tuple[dict[str, Any], dict[str, Any]]:
    try:
        return validate_job_outputs(job, attempt), {}
    except Exception:
        normalization = _legacy_status_normalization(summary)
        if normalization is None:
            raise
    normalized = dict(summary) | normalization
    with tempfile.TemporaryDirectory(prefix="agx-outcome-audit-") as temporary:
        view = Path(temporary)
        (view / "trial_summary.json").write_text(
            json.dumps(normalized, sort_keys=True), encoding="utf-8"
        )
        for name in orchestrator.required_outputs:
            if name == "trial_summary.json":
                continue
            (view / name).symlink_to((attempt / name).resolve())
        return validate_job_outputs(job, view), normalization


def audit(config_path: Path, repo_root: Path, output_root: Path) -> dict[str, Any]:
    orchestrator = CampaignOrchestrator(config_path, repo_root, logical_cores=22)
    jobs = {job.job_id: job for job in orchestrator.plan_jobs()}
    completed_root = output_root / "completed"
    attempts_root = output_root / "attempts"
    records: list[dict[str, Any]] = []
    rejected: list[dict[str, str]] = []

    if not attempts_root.is_dir():
        raise FileNotFoundError(f"attempt root does not exist: {attempts_root}")

    for parent in sorted(path for path in attempts_root.iterdir() if path.is_dir()):
        job_id = parent.name
        if (completed_root / job_id).exists() or job_id not in jobs:
            continue
        candidates = sorted(
            path
            for path in parent.iterdir()
            if path.is_dir()
            and (path / "failure.json").is_file()
            and (path / "trial_summary.json").is_file()
        )
        if not candidates:
            continue
        attempt = candidates[-1]
        summary = _read_object(attempt / "trial_summary.json")
        failure_path = attempt / "failure.json"
        failure = _read_object(failure_path)
        if not (
            failure.get("return_code") == 0
            and summary.get("causal_timing_enabled") is True
            and summary.get("algorithmic_status") == "incomplete"
            and summary.get("all_tasks_completed") is False
        ):
            continue
        try:
            validation, normalization = _validate_retained(
                orchestrator, jobs[job_id], attempt, summary
            )
        except Exception as error:
            rejected.append(
                {
                    "job_id": job_id,
                    "attempt": str(attempt),
                    "error_type": type(error).__name__,
                    "message": str(error),
                }
            )
            continue
        if validation.get("algorithmic_status") != "incomplete":
            rejected.append(
                {
                    "job_id": job_id,
                    "attempt": str(attempt),
                    "error_type": "UnexpectedClassification",
                    "message": "validated outcome was not algorithmically incomplete",
                }
            )
            continue

        required_hashes = {
            name: _sha256(attempt / name) for name in orchestrator.required_outputs
        }
        job_path = attempt / "job.json"
        records.append(
            {
                "job_id": job_id,
                "condition_id": jobs[job_id].condition_id,
                "algorithm": jobs[job_id].algorithm,
                "arrival_load": jobs[job_id].load_id,
                "policy_id": jobs[job_id].policy_id,
                "trace_id": jobs[job_id].trace_id,
                "classification": "legitimate_algorithmic_incomplete",
                "algorithmic_failure_type": validation.get(
                    "algorithmic_failure_type"
                ),
                "technical_status": "completed",
                "runner_return_code": failure.get("return_code"),
                "retained_attempt": str(attempt),
                "historical_failure_message": failure.get("message"),
                "historical_failure_sha256": _sha256(failure_path),
                "legacy_status_alias_normalization": normalization or None,
                "job_record_sha256": _sha256(job_path) if job_path.is_file() else None,
                "required_output_sha256": required_hashes,
                "semantic_validation": validation,
            }
        )

    return {
        "schema_version": 1,
        "report_kind": "retained_algorithmic_outcome_audit",
        "generated_at": dt.datetime.now(dt.timezone.utc).isoformat().replace("+00:00", "Z"),
        "config_path": str(config_path),
        "config_sha256": _sha256(config_path),
        "output_root": str(output_root),
        "validation_source": str(Path(__file__).resolve()),
        "validation_module_sha256": _sha256(repo_root / "study/campaign.py"),
        "classification_rule": (
            "technically completed causal run with an explicit predeclared "
            "algorithmic horizon; unreleased task timestamps may be empty only "
            "when their manifest release is later than the observation horizon"
        ),
        "campaign_data_was_modified": False,
        "simulations_launched": 0,
        "legitimate_algorithmic_incomplete_count": len(records),
        "rejected_candidate_count": len(rejected),
        "outcomes": records,
        "rejected_candidates": rejected,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--repo-root", type=Path, default=Path("."))
    parser.add_argument("--output-root", type=Path, required=True)
    parser.add_argument("--report", type=Path)
    args = parser.parse_args()
    repo_root = args.repo_root.resolve()
    config_path = args.config.resolve()
    output_root = args.output_root.resolve()
    report_path = (
        args.report.resolve()
        if args.report is not None
        else output_root / "retained_algorithmic_outcomes.json"
    )
    report = audit(config_path, repo_root, output_root)
    _atomic_json(report_path, report)
    print(
        f"legitimate={report['legitimate_algorithmic_incomplete_count']} "
        f"rejected={report['rejected_candidate_count']} report={report_path}"
    )
    return 0 if not report["rejected_candidates"] else 2


if __name__ == "__main__":
    raise SystemExit(main())
