from __future__ import annotations

import csv
import json
import math
import statistics
from collections import defaultdict
from pathlib import Path
from typing import Any, Iterable

from allocator_replay.hil.manifest import (
    load_invalidation,
    load_manifest,
    sha256_file,
)


TIMINGS = (
    "allocator_time_us",
    "candidate_filter_time_us",
    "allocator_exclusive_time_us",
)


def _read_journals(root: Path) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for path in sorted((root / "journals").glob("*.jsonl")):
        with path.open("r", encoding="utf-8") as handle:
            for line in handle:
                if line.strip():
                    rows.append(json.loads(line))
    return rows


def _p95(values: list[float]) -> float:
    ordered = sorted(values)
    return ordered[max(0, math.ceil(0.95 * len(ordered)) - 1)]


def _stats(rows: list[dict[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {
        "allocator_call_count": len(rows),
        "timed_allocator_call_count": sum(
            row.get("allocator_time_us") is not None for row in rows
        ),
        "calls_with_filter": sum(
            int(row.get("candidate_filter_calls") or 0) > 0 for row in rows
        ),
        "candidate_filter_invocation_count": sum(
            int(row.get("candidate_filter_calls") or 0) for row in rows
        ),
    }
    classes: dict[str, int] = defaultdict(int)
    for row in rows:
        classes[str(row.get("call_class") or "unclassified")] += 1
    result["call_class_counts"] = ";".join(
        f"{key}:{value}" for key, value in sorted(classes.items())
    )
    for field in TIMINGS:
        values = [
            float(row[field]) for row in rows if row.get(field) is not None
        ]
        prefix = field.removesuffix("_us")
        result[f"{prefix}_total_us"] = sum(values) if values else 0
        result[f"{prefix}_mean_us"] = (
            statistics.fmean(values) if values else 0
        )
        result[f"{prefix}_median_us"] = (
            statistics.median(values) if values else 0
        )
        result[f"{prefix}_p95_us"] = _p95(values) if values else 0
        result[f"{prefix}_max_us"] = max(values) if values else 0
    return result


def _write(path: Path, rows: Iterable[dict[str, Any]]) -> None:
    rows = list(rows)
    path.parent.mkdir(parents=True, exist_ok=True)
    fields: list[str] = []
    for row in rows:
        for key in row:
            if key not in fields:
                fields.append(key)
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)


def _provenance_fields(
    root: Path,
    manifest: dict[str, Any],
    *,
    mission: str = "",
    trial_id: int | None = None,
    invalidation: dict[str, Any] | None = None,
    journaled_at: float | None = None,
) -> dict[str, Any]:
    frozen_manifest_path = root / "manifest.json"
    if not frozen_manifest_path.exists():
        frozen_manifest_path = root / "schedule.json"
    implementation = manifest.get("implementation", {})
    if journaled_at is not None:
        for segment in manifest.get("implementation_segments", []):
            started = float(segment.get("started_at_epoch", 0))
            ended_value = segment.get("ended_at_epoch")
            ended = float(ended_value) if ended_value is not None else None
            if journaled_at >= started and (ended is None or journaled_at < ended):
                implementation = segment.get("implementation", implementation)
                break
    device_build = implementation.get("device_build", {})
    simulator = implementation.get("simulator_sources", {}).get(mission, {})
    scenario_sha = ""
    sources = manifest.get("scenario_sources", {})
    if mission == "bayesian":
        scenario_sha = str(sources.get("bayesian", {}).get("sha256", ""))
    elif mission == "collaborative" and trial_id is not None:
        scenario_sha = str(
            sources.get("collaborative", {})
            .get(str(trial_id), {})
            .get("sha256", "")
        )
    return {
        "campaign_id": str(manifest.get("campaign_id", root.name)),
        "campaign_manifest_schema": manifest.get("schema", ""),
        "campaign_manifest_sha256": sha256_file(frozen_manifest_path),
        "implementation_id": str(
            implementation.get("implementation_id", "legacy_unspecified")
        ),
        "campaign_build_id": str(device_build.get("build_id", "")),
        "build_manifest_sha256": str(
            device_build.get("manifest_sha256", "")
        ),
        "allocator_source_bundle_sha256": str(
            device_build.get("source_bundle_sha256", "")
        ),
        "deployed_module_set_sha256": str(
            device_build.get("deployed_module_set_sha256", "")
        ),
        "host_source_sha256": str(
            implementation.get("host_source", {}).get("sha256", "")
        ),
        "simulator_source_sha256": str(simulator.get("sha256", "")),
        "scenario_sha256": scenario_sha,
        "analysis_valid": invalidation is None,
        "invalidation_reason": (
            "" if invalidation is None
            else str(invalidation.get("reason_code", "unspecified"))
        ),
    }


def _enrich(
    root: Path,
    manifest: dict[str, Any],
    row: dict[str, Any],
    invalidation: dict[str, Any] | None,
) -> dict[str, Any]:
    item = dict(row)
    mission = str(item.get("mission", ""))
    trial_value = item.get("trial_id")
    trial_id = int(trial_value) if trial_value is not None else None
    provenance = _provenance_fields(
        root,
        manifest,
        mission=mission,
        trial_id=trial_id,
        invalidation=invalidation,
        journaled_at=(
            float(item["journaled_at"])
            if item.get("journaled_at") is not None else None
        ),
    )
    # Journaled values identify what actually ran; frozen campaign fields fill
    # metadata that was deliberately not duplicated into every JSONL record.
    for key, value in provenance.items():
        if key == "scenario_sha256" and item.get(key):
            continue
        item[key] = value
    condition_id = str(item.get("condition_id", ""))
    job_lookup = manifest.get("_report_condition_lookup")
    if job_lookup is None:
        job_lookup = {
            str(candidate.get("condition_id", "")): candidate
            for candidate in manifest.get("jobs", [])
        }
        manifest["_report_condition_lookup"] = job_lookup
    job = job_lookup.get(condition_id, {})
    item["historical_system_csv"] = str(
        job.get(
            "historical_system_csv",
            item.get("historical_system_csv", ""),
        )
    )
    item["historical_system_sha256"] = str(
        job.get(
            "historical_system_sha256",
            item.get("historical_system_sha256", ""),
        )
    )
    return item


def rebuild_hil_reports(root: Path) -> dict[str, Any]:
    root = root.resolve()
    schedule = load_manifest(root)
    manifest = (
        load_manifest(root, "manifest.json")
        if (root / "manifest.json").exists()
        else schedule
    )
    invalidation = load_invalidation(root)
    journal = _read_journals(root)
    excluded_condition_ids = {
        str(job["condition_id"])
        for job in schedule["jobs"]
        if job.get("excluded_from_core") or job.get("status") == "excluded"
    }
    core_journal = [
        row
        for row in journal
        if str(row.get("condition_id", "")) not in excluded_condition_ids
    ]
    classifications = {
        (row["fixture_id"], int(row["run_generation"])): row["call_class"]
        for row in core_journal
        if row.get("record_type") == "call_classification"
    }
    trials = [
        row
        for row in core_journal
        if row.get("record_type") == "trial_complete"
    ]
    trial_failures = [
        _enrich(root, manifest, row, invalidation)
        for row in core_journal
        if row.get("record_type") == "trial_failed"
    ]
    journaled_failure_keys = {
        (str(row.get("condition_id", "")), int(row.get("trial_id", -1)))
        for row in trial_failures
    }
    for job in schedule["jobs"]:
        if job.get("excluded_from_core") or job.get("status") == "excluded":
            continue
        for failure in job.get("failed_trials", []):
            trial_id = int(failure["trial_id"])
            key = (str(job["condition_id"]), trial_id)
            if key in journaled_failure_keys:
                continue
            trial_failures.append(
                _enrich(
                    root,
                    manifest,
                    {
                        "schema": 1,
                        "record_type": "trial_failed",
                        "failure_source": "schedule_legacy",
                        "condition_id": job["condition_id"],
                        "mission": job["mission"],
                        "algorithm": job["algorithm"],
                        "top_k_level": job["top_k_level"],
                        "top_k_rate": job["top_k_rate"],
                        "top_k_cells": job["top_k_cells"],
                        "trial_id": trial_id,
                        "run_generation": failure.get("run_generation", ""),
                        "device_id": failure.get("device_id", ""),
                        "trial_status": "failed",
                        "failure_reason": failure.get("reason", "unspecified"),
                    },
                    invalidation,
                )
            )
            journaled_failure_keys.add(key)
    watchdog_adjustments = [
        _enrich(root, manifest, row, invalidation)
        for row in core_journal
        if row.get("record_type") == "watchdog_threshold_adjustment"
    ]
    complete_keys = {
        (
            row["condition_id"],
            int(row["trial_id"]),
            int(row["run_generation"]),
        )
        for row in trials
    }
    raw_calls: list[dict[str, Any]] = []
    raw_phases = [
        _enrich(root, manifest, row, invalidation)
        for row in core_journal
        if row.get("record_type") == "call_phase"
    ]
    accepted: list[dict[str, Any]] = []
    for row in core_journal:
        if row.get("record_type") != "call_attempt":
            continue
        item = _enrich(root, manifest, row, invalidation)
        item["call_class"] = classifications.get(
            (row["fixture_id"], int(row["run_generation"])),
            str(row.get("call_class") or ""),
        )
        item["accepted_for_analysis"] = bool(
            invalidation is None
            and
            row.get("accepted")
            and (
                row["condition_id"],
                int(row["trial_id"]),
                int(row["run_generation"]),
            )
            in complete_keys
        )
        raw_calls.append(item)
        if item["accepted_for_analysis"]:
            accepted.append(item)
    reports = root / "reports"
    _write(reports / "raw_call_attempts.csv", raw_calls)
    _write(reports / "raw_call_phases.csv", raw_phases)
    _write(reports / "trial_failures.csv", trial_failures)
    _write(reports / "watchdog_threshold_adjustments.csv", watchdog_adjustments)

    robot_groups: dict[tuple[Any, ...], list[dict[str, Any]]] = defaultdict(list)
    system_groups: dict[tuple[Any, ...], list[dict[str, Any]]] = defaultdict(list)
    for row in accepted:
        system_key = (
            row["condition_id"],
            int(row["trial_id"]),
            int(row["run_generation"]),
        )
        system_groups[system_key].append(row)
        robot_groups[system_key + (row["robot_id"],)].append(row)
    trial_lookup = {
        (
            row["condition_id"],
            int(row["trial_id"]),
            int(row["run_generation"]),
        ): row
        for row in trials
    }
    robot_rows: list[dict[str, Any]] = []
    for key, rows in sorted(robot_groups.items()):
        condition_id, trial_id, generation, robot_id = key
        meta = rows[0]
        robot_rows.append(
            _enrich(root, manifest, {
                "condition_id": condition_id,
                "mission": meta["mission"],
                "algorithm": meta["algorithm"],
                "top_k_level": meta["top_k_level"],
                "top_k_rate": meta["top_k_rate"],
                "top_k_cells": meta["top_k_cells"],
                "trial_id": trial_id,
                "run_generation": generation,
                "robot_id": robot_id,
                "device_id": meta["device_id"],
                "build_id": meta.get("build_id", ""),
                "journaled_at": min(
                    float(row.get("journaled_at", 0)) for row in rows
                ),
                **_stats(rows),
            }, invalidation)
        )
    _write(reports / "robot_trial_metrics.csv", robot_rows)

    system_rows: list[dict[str, Any]] = []
    for key, rows in sorted(system_groups.items()):
        trial = trial_lookup[key]
        meta = rows[0]
        system_rows.append(
            _enrich(root, manifest, {
                "condition_id": key[0],
                "mission": meta["mission"],
                "algorithm": meta["algorithm"],
                "top_k_level": meta["top_k_level"],
                "top_k_rate": meta["top_k_rate"],
                "top_k_cells": meta["top_k_cells"],
                "trial_id": key[1],
                "run_generation": key[2],
                "device_id": meta["device_id"],
                "build_id": meta.get("build_id", ""),
                "journaled_at": float(
                    trial.get("journaled_at", meta.get("journaled_at", 0))
                ),
                "scenario_file": trial["scenario_file"],
                "scenario_sha256": trial["scenario_sha256"],
                "trial_status": trial["trial_status"],
                "total_team_steps": trial["total_team_steps"],
                "max_steps_any_robot": trial["max_steps_any_robot"],
                "events_processed": trial["events_processed"],
                **_stats(rows),
            }, invalidation)
        )
    _write(reports / "system_trial_metrics.csv", system_rows)

    by_condition: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in accepted:
        by_condition[row["condition_id"]].append(row)
    condition_rows: list[dict[str, Any]] = []
    for job in schedule["jobs"]:
        if job.get("excluded_from_core") or job.get("status") == "excluded":
            continue
        rows = by_condition.get(job["condition_id"], [])
        representative = (
            job["status"] == "complete"
            and not job.get("failed_trials")
            and invalidation is None
        )
        condition_calls = by_condition.get(job["condition_id"], [])
        base = _enrich(root, manifest, {
            "condition_id": job["condition_id"],
            "mission": job["mission"],
            "algorithm": job["algorithm"],
            "top_k_level": job["top_k_level"],
            "top_k_rate": job["top_k_rate"],
            "top_k_cells": job["top_k_cells"],
            "status": job["status"],
            "stopped_reason": job.get("stopped_reason", ""),
            "representative": representative,
            "completed_trial_count": len(job["completed_trials"]),
            "failed_trial_count": len(job.get("failed_trials", [])),
            "planned_trial_count": len(job["trial_ids"]),
            "historical_system_csv": job.get("historical_system_csv", ""),
            "historical_system_sha256": job.get(
                "historical_system_sha256", ""
            ),
            "device_ids": ";".join(
                sorted(
                    {
                        str(row.get("device_id", ""))
                        for row in condition_calls
                        if row.get("device_id")
                    }
                )
            ),
            "mixed_device_condition": len(
                {
                    str(row.get("device_id", ""))
                    for row in condition_calls
                    if row.get("device_id")
                }
            ) > 1,
            "build_ids": ";".join(
                sorted(
                    {
                        str(row.get("build_id", ""))
                        for row in condition_calls
                        if row.get("build_id")
                    }
                )
            ),
        }, invalidation)
        if representative:
            base.update(_stats(rows))
        condition_rows.append(base)
    _write(reports / "condition_metrics.csv", condition_rows)
    return {
        "raw_attempts": len(raw_calls),
        "raw_phases": len(raw_phases),
        "accepted_calls": len(accepted),
        "completed_trials": len(system_rows),
        "failed_trials": len(trial_failures),
        "watchdog_adjustments": len(watchdog_adjustments),
        "excluded_conditions": len(excluded_condition_ids),
        "condition_rows": len(condition_rows),
        "analysis_valid": invalidation is None,
        "invalidation": invalidation,
        "campaign_manifest_sha256": sha256_file(
            root / (
                "manifest.json"
                if (root / "manifest.json").exists()
                else "schedule.json"
            )
        ),
        "reports": str(reports),
    }
