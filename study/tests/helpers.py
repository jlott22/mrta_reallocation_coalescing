from __future__ import annotations

import csv
import json
import math
import statistics
from pathlib import Path
from typing import Any


def test_config(
    output_root: str = "study/output/test",
    algorithms: list[str] | None = None,
    policies: list[dict[str, Any]] | None = None,
    trace_count: int = 2,
    loads: dict[str, float] | None = None,
) -> dict[str, Any]:
    arrival_loads = loads or {"low": 0.1, "high": 0.4}
    return {
        "manifest": {
            "manifest_set_id": "test_set_v1",
            "root": "study/generated/manifests",
            "master_seed": 123456,
            "trace_count": trace_count,
            "grid_size": 19,
            "robot_count": 4,
            "task_count": 12,
            "initial_task_count": 8,
            "arrival_loads": arrival_loads,
        },
        "campaign": {
            "campaign_id": "test_campaign",
            "output_root": output_root,
            "algorithms": algorithms or ["CBAA"],
            "loads": list(arrival_loads),
            "policies": policies or [
                {"policy_id": "eager_b1", "mode": "eager", "batch_size": 1},
                {"policy_id": "count_b2", "mode": "count", "batch_size": 2},
            ],
            "max_workers": 8,
            "required_outputs": [
                "trial_summary.json", "task_events.csv", "allocation_epochs.csv"
            ],
            "runner": {"module": "known_visit_sim.run_online_trials", "extra_args": []},
        },
    }


def write_config(root: Path, config: dict[str, Any]) -> Path:
    path = root / "config.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(config, indent=2), encoding="utf-8")
    return path


def _nearest_rank(values: list[float], proportion: float) -> float:
    ordered = sorted(values)
    return ordered[max(0, math.ceil(proportion * len(ordered)) - 1)] if ordered else 0.0


def _write_csv(path: Path, rows: list[dict[str, Any]]) -> None:
    fields = list(rows[0])
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader()
        for row in rows:
            writer.writerow({
                key: json.dumps(value, separators=(",", ":")) if isinstance(value, list) else value
                for key, value in row.items()
            })


def write_valid_outputs(
    job: Any,
    attempt_dir: Path,
    *,
    all_completed: bool = True,
    allocator_time_s: float = 1.0,
    assignment_delay_s: float = 0.2,
    completion_delay_s: float = 1.0,
    last_task_unreleased: bool = False,
) -> None:
    """Write a small but semantically complete synthetic online-trial result."""

    if last_task_unreleased and all_completed:
        raise ValueError("an unreleased task requires an incomplete outcome")

    release = json.loads(job.release_path.read_text(encoding="utf-8"))
    task_rows: list[dict[str, Any]] = []
    assignment_latencies: list[float] = []
    completion_latencies: list[float] = []
    for index, task in enumerate(release["tasks"], start=1):
        release_s = float(task["release_time_s"])
        unreleased = last_task_unreleased and index == len(release["tasks"])
        admission_s = None if unreleased else (release_s if release_s == 0 else release_s + 0.1)
        assignment_s = None if admission_s is None else admission_s + assignment_delay_s
        incomplete = not all_completed and index == len(release["tasks"])
        completion_s = None if incomplete else assignment_s + completion_delay_s
        if assignment_s is not None:
            assignment_latencies.append(assignment_s - release_s)
        if completion_s is not None:
            completion_latencies.append(completion_s - release_s)
        task_rows.append({
            "trial_id": job.trace_id,
            "task_id": task["task_id"],
            "task_index": index,
            "task_x": task["x"],
            "task_y": task["y"],
            "state": "unreleased" if unreleased else ("assigned" if incomplete else "completed"),
            "release_time_s": "" if unreleased else release_s,
            "admission_time_s": "" if admission_s is None else admission_s,
            "first_assignment_time_s": "" if assignment_s is None else assignment_s,
            "first_assigned_robot": "" if assignment_s is None else "00",
            "completion_time_s": "" if completion_s is None else completion_s,
            "completing_robot": "" if completion_s is None else "00",
            "assignment_events": 0 if assignment_s is None else 1,
            "release_to_admission_latency_s": (
                "" if admission_s is None else admission_s - release_s
            ),
            "release_to_first_assignment_latency_s": (
                "" if assignment_s is None else assignment_s - release_s
            ),
            "admission_to_first_assignment_latency_s": (
                "" if assignment_s is None or admission_s is None else assignment_s - admission_s
            ),
            "release_to_completion_latency_s": (
                "" if completion_s is None else completion_s - release_s
            ),
            "assignment_to_completion_latency_s": (
                "" if completion_s is None else completion_s - assignment_s
            ),
        })
    initial_ids = [str(task["task_id"]) for task in release["tasks"] if task["initially_visible"]]
    online_ids = [
        str(task["task_id"])
        for index, task in enumerate(release["tasks"], start=1)
        if not task["initially_visible"]
        and not (last_task_unreleased and index == len(release["tasks"]))
    ]
    observed_tasks = release["tasks"][:-1] if last_task_unreleased else release["tasks"]
    last_release = max(float(task["release_time_s"]) for task in observed_tasks)
    epoch_rows = [
        {
            "epoch_id": 1,
            "opened_time_s": 0.0,
            "trigger_reason": "initial_allocation",
            "mandatory": "true",
            "admitted_count": len(initial_ids),
            "admitted_task_ids": initial_ids,
            "allocator_call_ids": [1],
            "allocator_time_s": allocator_time_s * 0.4,
            "closed_time_s": 0.1,
        },
        {
            "epoch_id": 2,
            "opened_time_s": last_release + 0.1,
            "trigger_reason": "final_release_flush",
            "mandatory": "false",
            "admitted_count": len(online_ids),
            "admitted_task_ids": online_ids,
            "allocator_call_ids": [2],
            "allocator_time_s": allocator_time_s * 0.6,
            "closed_time_s": last_release + 0.2,
        },
    ]
    completed_count = len(task_rows) if all_completed else len(task_rows) - 1
    summary = {
        "schema_version": 1,
        "trial_id": job.trace_id,
        "condition_id": job.condition_id,
        "algorithm": job.algorithm,
        "arrival_load": job.load_id,
        "policy_id": job.policy_id,
        "policy_mode": job.policy_mode,
        "batch_size": job.batch_size,
        "policy_max_pending_age_s": job.max_pending_age_s,
        "runtime_seed": job.runtime_seed,
        "python_hash_seed": job.python_hash_seed,
        "scenario_sha256": job.scenario_sha256,
        "release_sha256": job.release_sha256,
        "all_tasks_completed": all_completed,
        "max_robot_steps": 10,
        "total_team_steps": 40,
        "movement_time_s": 100.0,
        "other_execution_time_s": 0.0,
        "simulated_execution_time_s": 100.0,
        "cumulative_allocator_time_s": allocator_time_s,
        "mission_elapsed_time_s": 100.0 + allocator_time_s,
        "host_program_runtime_s": 0.5,
        "allocator_call_count": 2,
        "allocation_epoch_count": 2,
        "mean_allocator_call_time_s": allocator_time_s * 0.5,
        "median_allocator_call_time_s": allocator_time_s * 0.5,
        "p95_allocator_call_time_s": allocator_time_s * 0.6,
        "max_allocator_call_time_s": allocator_time_s * 0.6,
        "allocator_time_per_completed_task_s": allocator_time_s / completed_count,
        "mean_release_to_first_assignment_latency_s": statistics.fmean(assignment_latencies),
        "median_release_to_first_assignment_latency_s": statistics.median(assignment_latencies),
        "p95_release_to_first_assignment_latency_s": _nearest_rank(assignment_latencies, 0.95),
        "mean_release_to_completion_latency_s": statistics.fmean(completion_latencies),
        "median_release_to_completion_latency_s": statistics.median(completion_latencies),
        "p95_release_to_completion_latency_s": _nearest_rank(completion_latencies, 0.95),
        "arrival_induced_trigger_count": 1,
        "mandatory_trigger_count": 1,
        "mean_pending_queue_depth": 1.0,
        "max_pending_queue_depth": 4,
        "mean_pending_age_s": 0.1,
        "max_pending_age_s": 0.2,
        "trigger_reason_counts": {"final_release_flush": 1, "initial_allocation": 1},
    }
    (attempt_dir / "trial_summary.json").write_text(json.dumps(summary), encoding="utf-8")
    _write_csv(attempt_dir / "task_events.csv", task_rows)
    _write_csv(attempt_dir / "allocation_epochs.csv", epoch_rows)
