"""Aggregate campaign outputs into trial-level paired and plotting-ready CSVs.

All inferential inputs are one row per paired *trial*.  Per-task rows are kept
for descriptive latency work only and are never treated as independent
replicates by this module.
"""

from __future__ import annotations

import argparse
import csv
import json
import math
import statistics
from collections import defaultdict
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

from study.campaign import CampaignJob, CampaignOrchestrator, utc_now
from study.manifests import canonical_json_bytes


DIMENSIONS = (
    "job_id", "condition_id", "trial_id", "algorithm", "arrival_load", "policy_id",
    "policy_mode", "batch_size", "policy_max_pending_age_s", "runtime_seed",
    "scenario_sha256", "release_sha256",
)

METRICS = (
    "all_tasks_completed",
    "max_robot_steps",
    "total_team_steps",
    "mission_elapsed_time_s",
    "movement_time_s",
    "other_execution_time_s",
    "cumulative_allocator_time_s",
    "allocator_parallel_critical_path_time_s",
    "mission_elapsed_time_serial_compute_s",
    "allocator_call_count",
    "allocation_epoch_count",
    "mean_allocator_call_time_s",
    "median_allocator_call_time_s",
    "p95_allocator_call_time_s",
    "max_allocator_call_time_s",
    "allocator_time_per_completed_task_s",
    "mean_release_to_first_assignment_latency_s",
    "median_release_to_first_assignment_latency_s",
    "p95_release_to_first_assignment_latency_s",
    "mean_release_to_completion_latency_s",
    "median_release_to_completion_latency_s",
    "p95_release_to_completion_latency_s",
    "arrival_induced_trigger_count",
    "mandatory_trigger_count",
    "mandatory_reallocation_trigger_count",
    "mean_pending_queue_depth",
    "max_pending_queue_depth",
    "mean_pending_age_s",
    "max_pending_age_s",
)

ALIASES = {
    "allocator_time_s": "cumulative_allocator_time_s",
    "cumulative_compute_s": "cumulative_allocator_time_s",
    "allocator_epoch_count": "allocation_epoch_count",
    "mean_assignment_latency_s": "mean_release_to_first_assignment_latency_s",
    "median_assignment_latency_s": "median_release_to_first_assignment_latency_s",
    "p95_assignment_latency_s": "p95_release_to_first_assignment_latency_s",
    "mean_completion_latency_s": "mean_release_to_completion_latency_s",
    "median_completion_latency_s": "median_release_to_completion_latency_s",
    "p95_completion_latency_s": "p95_release_to_completion_latency_s",
}

PAIRED_METRICS = {
    "cumulative_allocator_time_s": ("allocator_compute_saved_s", "baseline_minus_condition"),
    "mean_release_to_first_assignment_latency_s": ("change_assignment_latency_s", "condition_minus_baseline"),
    "mean_release_to_completion_latency_s": ("change_completion_latency_s", "condition_minus_baseline"),
    "mission_elapsed_time_s": ("change_mission_elapsed_time_s", "condition_minus_baseline"),
    "max_robot_steps": ("change_max_robot_steps", "condition_minus_baseline"),
    "allocation_epoch_count": ("change_allocation_epochs", "condition_minus_baseline"),
    "allocator_call_count": ("change_allocator_calls", "condition_minus_baseline"),
}


def _read_json_object(path: Path) -> dict[str, Any]:
    with path.open(encoding="utf-8") as handle:
        value = json.load(handle)
    if not isinstance(value, dict):
        raise ValueError(f"expected a JSON object: {path}")
    return value


def _read_csv(path: Path) -> list[dict[str, str]]:
    with path.open(newline="", encoding="utf-8-sig") as handle:
        return list(csv.DictReader(handle))


def _write_csv(path: Path, rows: Sequence[Mapping[str, Any]], preferred: Sequence[str] = ()) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    seen = set(preferred)
    fields = list(preferred)
    for row in rows:
        for key in row:
            if key not in seen:
                fields.append(key)
                seen.add(key)
    with path.open("w", newline="", encoding="utf-8") as handle:
        if not fields:
            handle.write("")
            return
        writer = csv.DictWriter(handle, fieldnames=fields, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)


def _number(value: Any) -> float | None:
    if value is None or value == "":
        return None
    if isinstance(value, bool):
        return float(value)
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def _truth(value: Any) -> bool | None:
    if isinstance(value, bool):
        return value
    if value is None or value == "":
        return None
    if isinstance(value, (int, float)):
        return bool(value)
    normalized = str(value).strip().lower()
    if normalized in {"true", "1", "yes"}:
        return True
    if normalized in {"false", "0", "no"}:
        return False
    return None


def _dimensions(job: CampaignJob) -> dict[str, Any]:
    return {
        "job_id": job.job_id,
        "condition_id": job.condition_id,
        "trial_id": job.trace_id,
        "algorithm": job.algorithm,
        "arrival_load": job.load_id,
        "policy_id": job.policy_id,
        "policy_mode": job.policy_mode,
        "batch_size": job.batch_size,
        "policy_max_pending_age_s": job.max_pending_age_s,
        "runtime_seed": job.runtime_seed,
        "scenario_sha256": job.scenario_sha256,
        "release_sha256": job.release_sha256,
    }


def _normalize_summary(summary: dict[str, Any], job: CampaignJob) -> dict[str, Any]:
    normalized = dict(summary)
    for alias, canonical in ALIASES.items():
        if canonical not in normalized and alias in normalized:
            normalized[canonical] = normalized[alias]
    expected = _dimensions(job)
    comparisons = {
        "job_id": expected["job_id"],
        "condition_id": expected["condition_id"],
        "trial_id": expected["trial_id"],
        "trace_id": expected["trial_id"],
        "algorithm": expected["algorithm"],
        "arrival_load": expected["arrival_load"],
        "load_id": expected["arrival_load"],
        "policy_id": expected["policy_id"],
    }
    for key, expected_value in comparisons.items():
        if key in summary and str(summary[key]) != str(expected_value):
            raise ValueError(
                f"trial summary dimension {key}={summary[key]!r} disagrees with campaign job "
                f"{expected_value!r}: {job.job_id}"
            )
    normalized.update(expected)
    normalized["technical_status"] = "completed"
    return normalized


def _timestamp(row: Mapping[str, Any], *names: str) -> float | None:
    for name in names:
        if name in row and row[name] not in {None, ""}:
            return _number(row[name])
    return None


def validate_task_rows(rows: Sequence[Mapping[str, Any]], job: CampaignJob) -> None:
    ids: set[str] = set()
    for row in rows:
        task_id = str(row.get("task_id", ""))
        if not task_id:
            raise ValueError(f"task row lacks task_id: {job.job_id}")
        if task_id in ids:
            raise ValueError(f"duplicate task_id {task_id!r}: {job.job_id}")
        ids.add(task_id)
        release = _timestamp(row, "release_time_s", "release_time")
        admission = _timestamp(row, "admission_time_s", "admission_time")
        assignment = _timestamp(row, "first_assignment_time_s", "first_assignment_time")
        completion = _timestamp(row, "completion_time_s", "completion_time")
        if release is None or release < 0:
            raise ValueError(f"invalid release timestamp for {task_id}: {job.job_id}")
        ordered = [value for value in (release, admission, assignment, completion) if value is not None]
        if ordered != sorted(ordered):
            raise ValueError(f"task timestamps out of order for {task_id}: {job.job_id}")


def _merge_child_rows(rows: Sequence[dict[str, str]], job: CampaignJob) -> list[dict[str, Any]]:
    dimensions = _dimensions(job)
    merged: list[dict[str, Any]] = []
    for raw in rows:
        row = dict(raw)
        for key in ("job_id", "condition_id", "trial_id", "algorithm", "arrival_load", "policy_id"):
            if key in row and row[key] not in {"", str(dimensions[key])}:
                raise ValueError(f"{key} mismatch in child CSV for {job.job_id}")
        row.update(dimensions)
        merged.append(row)
    return merged


def _percentile(values: Sequence[float], probability: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    if len(ordered) == 1:
        return ordered[0]
    position = probability * (len(ordered) - 1)
    lower = math.floor(position)
    upper = math.ceil(position)
    if lower == upper:
        return ordered[lower]
    fraction = position - lower
    return ordered[lower] * (1 - fraction) + ordered[upper] * fraction


def _describe(rows: Sequence[Mapping[str, Any]], metric: str) -> dict[str, Any]:
    values = [number for row in rows if (number := _number(row.get(metric))) is not None]
    if not values:
        return {
            f"{metric}_n": 0,
            f"{metric}_mean": None,
            f"{metric}_median": None,
            f"{metric}_sd": None,
            f"{metric}_p95": None,
            f"{metric}_min": None,
            f"{metric}_max": None,
        }
    return {
        f"{metric}_n": len(values),
        f"{metric}_mean": statistics.fmean(values),
        f"{metric}_median": statistics.median(values),
        f"{metric}_sd": statistics.stdev(values) if len(values) > 1 else None,
        f"{metric}_p95": _percentile(values, 0.95),
        f"{metric}_min": min(values),
        f"{metric}_max": max(values),
    }


def paired_deltas(trial_rows: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    """Return within-trace changes against the unique Eager/B=1 row."""

    groups: dict[tuple[str, str, str], list[Mapping[str, Any]]] = defaultdict(list)
    for row in trial_rows:
        if row.get("technical_status") == "completed":
            groups[(str(row["algorithm"]), str(row["arrival_load"]), str(row["trial_id"]))].append(row)
    output: list[dict[str, Any]] = []
    for key, rows in sorted(groups.items()):
        eager = [
            row for row in rows
            if row.get("policy_mode") == "eager" and _number(row.get("batch_size")) == 1
        ]
        if len(eager) != 1:
            # An incomplete pair is retained in trial_level.csv but cannot enter
            # paired inference. Multiple eager baselines are a design error.
            if len(eager) > 1:
                raise ValueError(f"multiple Eager/B=1 baselines for paired key {key}")
            continue
        baseline = eager[0]
        for condition in sorted(rows, key=lambda row: str(row["policy_id"])):
            paired: dict[str, Any] = {
                name: condition.get(name)
                for name in DIMENSIONS
                if name in condition
            }
            paired["baseline_policy_id"] = baseline["policy_id"]
            paired["baseline_condition_id"] = baseline["condition_id"]
            paired["pair_complete"] = True
            for metric, (delta_name, direction) in PAIRED_METRICS.items():
                baseline_value = _number(baseline.get(metric))
                condition_value = _number(condition.get(metric))
                paired[f"eager_{metric}"] = baseline_value
                paired[f"condition_{metric}"] = condition_value
                if baseline_value is None or condition_value is None:
                    paired[delta_name] = None
                elif direction == "baseline_minus_condition":
                    paired[delta_name] = baseline_value - condition_value
                else:
                    paired[delta_name] = condition_value - baseline_value
            eager_compute = _number(baseline.get("cumulative_allocator_time_s"))
            compute = _number(condition.get("cumulative_allocator_time_s"))
            paired["percent_allocator_computation_saved"] = (
                None if eager_compute in {None, 0.0} or compute is None
                else 100.0 * (eager_compute - compute) / eager_compute
            )
            baseline_completed = _truth(baseline.get("all_tasks_completed"))
            condition_completed = _truth(condition.get("all_tasks_completed"))
            paired["eager_all_tasks_completed"] = baseline_completed
            paired["condition_all_tasks_completed"] = condition_completed
            output.append(paired)
    return output


def condition_summaries(
    trial_rows: Sequence[Mapping[str, Any]],
    paired_rows: Sequence[Mapping[str, Any]],
) -> list[dict[str, Any]]:
    group_names = (
        "algorithm", "arrival_load", "policy_id", "policy_mode", "batch_size",
        "policy_max_pending_age_s",
    )
    groups: dict[tuple[Any, ...], list[Mapping[str, Any]]] = defaultdict(list)
    for row in trial_rows:
        groups[tuple(row.get(name) for name in group_names)].append(row)
    paired_groups: dict[tuple[Any, ...], list[Mapping[str, Any]]] = defaultdict(list)
    for row in paired_rows:
        paired_groups[tuple(row.get(name) for name in group_names)].append(row)
    output: list[dict[str, Any]] = []
    for key, planned in sorted(groups.items(), key=lambda item: tuple(str(value) for value in item[0])):
        completed = [row for row in planned if row.get("technical_status") == "completed"]
        mission_completed = [row for row in completed if _truth(row.get("all_tasks_completed")) is True]
        summary = dict(zip(group_names, key, strict=True))
        summary.update({
            "planned_trial_count": len(planned),
            "runner_success_count": len(completed),
            "technical_failure_count": len(planned) - len(completed),
            "runner_success_rate": len(completed) / len(planned) if planned else None,
            "mission_completion_count": len(mission_completed),
            "mission_completion_rate_among_runner_successes": (
                len(mission_completed) / len(completed) if completed else None
            ),
            "paired_trial_count": len(paired_groups.get(key, [])),
        })
        for metric in METRICS:
            if metric == "all_tasks_completed":
                continue
            summary.update(_describe(completed, metric))
        paired_metric_names = [
            "allocator_compute_saved_s",
            "percent_allocator_computation_saved",
            "change_assignment_latency_s",
            "change_completion_latency_s",
            "change_mission_elapsed_time_s",
            "change_max_robot_steps",
            "change_allocation_epochs",
            "change_allocator_calls",
        ]
        for metric in paired_metric_names:
            summary.update(_describe(paired_groups.get(key, []), metric))
        output.append(summary)
    return output


def _figure_rows(condition_rows: Sequence[Mapping[str, Any]]) -> tuple[list[dict[str, Any]], ...]:
    figure1: list[dict[str, Any]] = []
    figure2: list[dict[str, Any]] = []
    figure3: list[dict[str, Any]] = []
    for row in condition_rows:
        dims = {
            "algorithm": row.get("algorithm"),
            "arrival_load": row.get("arrival_load"),
            "policy_id": row.get("policy_id"),
            "policy_mode": row.get("policy_mode"),
            "batch_size": row.get("batch_size"),
            "policy_max_pending_age_s": row.get("policy_max_pending_age_s"),
            "planned_trial_count": row.get("planned_trial_count"),
            "runner_success_count": row.get("runner_success_count"),
            "paired_trial_count": row.get("paired_trial_count"),
        }
        figure1.append(dims | {
            "x_mean_change_completion_latency_s": row.get("change_completion_latency_s_mean"),
            "x_median_change_completion_latency_s": row.get("change_completion_latency_s_median"),
            "y_mean_percent_allocator_computation_saved": row.get(
                "percent_allocator_computation_saved_mean"
            ),
            "y_median_percent_allocator_computation_saved": row.get(
                "percent_allocator_computation_saved_median"
            ),
            "mean_change_assignment_latency_s": row.get("change_assignment_latency_s_mean"),
        })
        figure2.append(dims | {
            "mean_cumulative_allocator_time_s": row.get("cumulative_allocator_time_s_mean"),
            "mean_allocation_epoch_count": row.get("allocation_epoch_count_mean"),
            "mean_allocator_call_count": row.get("allocator_call_count_mean"),
            "mean_allocator_compute_saved_s": row.get("allocator_compute_saved_s_mean"),
            "mission_completion_rate": row.get("mission_completion_rate_among_runner_successes"),
        })
        figure3.append(dims | {
            "mean_percent_allocator_computation_saved": row.get(
                "percent_allocator_computation_saved_mean"
            ),
            "mean_change_assignment_latency_s": row.get("change_assignment_latency_s_mean"),
            "mean_change_completion_latency_s": row.get("change_completion_latency_s_mean"),
            "mean_change_mission_elapsed_time_s": row.get("change_mission_elapsed_time_s_mean"),
            "mission_completion_rate": row.get("mission_completion_rate_among_runner_successes"),
            "technical_failure_count": row.get("technical_failure_count"),
        })
    return figure1, figure2, figure3


def aggregate(orchestrator: CampaignOrchestrator, analysis_dir: Path | None = None) -> dict[str, Path]:
    """Aggregate all planned jobs and return paths to generated artifacts."""

    jobs = orchestrator.plan_jobs()
    destination = analysis_dir or (orchestrator.output_root / "analysis")
    destination = destination.resolve()
    destination.mkdir(parents=True, exist_ok=True)
    trial_rows: list[dict[str, Any]] = []
    task_rows: list[dict[str, Any]] = []
    epoch_rows: list[dict[str, Any]] = []
    failure_rows: list[dict[str, Any]] = []
    for job in jobs:
        dimensions = _dimensions(job)
        completed_dir = orchestrator.completed_dir(job)
        state = orchestrator._completion_state(job)
        if state != "completed":
            trial_rows.append(dimensions | {
                "technical_status": "failed_or_missing" if state == "pending" else "conflict",
                "all_tasks_completed": None,
            })
            failure_rows.append(dimensions | {"technical_status": state})
            continue
        summary = _normalize_summary(_read_json_object(completed_dir / "trial_summary.json"), job)
        trial_rows.append(summary)
        per_task = _read_csv(completed_dir / "task_events.csv")
        validate_task_rows(per_task, job)
        task_rows.extend(_merge_child_rows(per_task, job))
        epoch_path = completed_dir / "allocation_epochs.csv"
        if epoch_path.is_file():
            epoch_rows.extend(_merge_child_rows(_read_csv(epoch_path), job))
    trial_rows.sort(key=lambda row: str(row["job_id"]))
    task_rows.sort(key=lambda row: (str(row["job_id"]), str(row.get("task_id", ""))))
    epoch_rows.sort(key=lambda row: (str(row["job_id"]), str(row.get("epoch_id", ""))))
    paired_rows = paired_deltas(trial_rows)
    condition_rows = condition_summaries(trial_rows, paired_rows)
    figure1, figure2, figure3 = _figure_rows(condition_rows)
    paths = {
        "trial_level": destination / "trial_level.csv",
        "task_level": destination / "task_level_descriptive_only.csv",
        "epoch_level": destination / "allocation_epoch_level.csv",
        "paired": destination / "paired_eager_deltas_trial_level.csv",
        "conditions": destination / "condition_summaries.csv",
        "failures": destination / "technical_failures_or_missing.csv",
        "figure1": destination / "figure1_compute_responsiveness.csv",
        "figure2": destination / "figure2_mechanism.csv",
        "figure3": destination / "figure3_arrival_load_sensitivity.csv",
    }
    _write_csv(paths["trial_level"], trial_rows, (*DIMENSIONS, "technical_status", *METRICS))
    _write_csv(paths["task_level"], task_rows, (*DIMENSIONS, "task_id"))
    _write_csv(paths["epoch_level"], epoch_rows, (*DIMENSIONS, "epoch_id", "trigger_reason"))
    _write_csv(paths["paired"], paired_rows, DIMENSIONS)
    _write_csv(paths["conditions"], condition_rows)
    _write_csv(paths["failures"], failure_rows, DIMENSIONS)
    _write_csv(paths["figure1"], figure1)
    _write_csv(paths["figure2"], figure2)
    _write_csv(paths["figure3"], figure3)
    metadata = {
        "schema_version": 1,
        "generated_at": utc_now(),
        "campaign_id": orchestrator.campaign["campaign_id"],
        "planned_trial_rows": len(trial_rows),
        "successful_trial_rows": sum(row.get("technical_status") == "completed" for row in trial_rows),
        "paired_trial_rows": len(paired_rows),
        "task_rows_are_descriptive_not_independent_replicates": True,
        "paired_baseline": "unique policy_mode=eager and batch_size=1 within algorithm/load/trial_id",
        "files": {name: str(path) for name, path in paths.items()},
    }
    metadata_path = destination / "analysis_metadata.json"
    metadata_path.write_bytes(canonical_json_bytes(metadata))
    paths["metadata"] = metadata_path
    return paths


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--repo-root", type=Path, default=Path("."))
    parser.add_argument("--analysis-dir", type=Path)
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    orchestrator = CampaignOrchestrator(args.config, args.repo_root)
    paths = aggregate(orchestrator, args.analysis_dir)
    for name, path in paths.items():
        print(f"{name}: {path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
