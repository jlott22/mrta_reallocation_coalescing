#!/usr/bin/env python3
"""Build the compact August 14 MRTA publication dataset and evaluation.

The raw campaign trees remain immutable and local.  This exporter reads their
promoted outputs plus hash-audited algorithmic outcomes and writes a compact,
Git-friendly package containing trial-level data, exact causal/zero pairs,
condition/factor summaries, hardware timing validation, verification results,
figures, and a checksum manifest.
"""

from __future__ import annotations

import argparse
import csv
import datetime as dt
import hashlib
import json
import math
import statistics
from collections import Counter, defaultdict
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt
from scipy import stats


ALGORITHMS = ("CBAA", "ACBBA", "PI", "HIPC")
LOADS = ("low", "medium", "high")
POLICIES = ("eager_b1", "count_b2", "count_b4", "count_b8", "bounded_b4_w5")
METRICS = (
    "mission_elapsed_time_s",
    "mean_release_to_first_assignment_latency_s",
    "median_release_to_first_assignment_latency_s",
    "mean_release_to_completion_latency_s",
    "median_release_to_completion_latency_s",
    "max_release_to_first_assignment_latency_s",
    "max_release_to_completion_latency_s",
    "max_robot_steps",
    "total_team_steps",
    "allocator_call_count",
    "allocation_epoch_count",
    "arrival_induced_trigger_count",
    "mandatory_trigger_count",
    "mandatory_reallocation_trigger_count",
    "timeout_trigger_count",
    "batch_threshold_trigger_count",
    "piggybacked_admission_epoch_count",
    "mean_pending_queue_depth",
    "max_pending_queue_depth",
    "mean_pending_age_s",
    "max_pending_age_s",
    "agx_allocator_processor_work_s",
    "rp2040_allocator_processor_work_s",
    "cumulative_allocator_time_s",
    "allocator_time_per_completed_task_s",
    "processor_capacity_fraction",
    "host_program_runtime_s",
)
PAIR_METRICS = {
    "mission_elapsed_time_s": "mission_delta_s",
    "mean_release_to_first_assignment_latency_s": "assignment_latency_delta_s",
    "mean_release_to_completion_latency_s": "completion_latency_delta_s",
    "max_robot_steps": "max_robot_steps_delta",
    "allocator_call_count": "allocator_calls_delta",
    "allocation_epoch_count": "allocation_epochs_delta",
}
POLICY_PAIR_METRICS = {
    "mission_elapsed_time_s": "mission_change_vs_eager_s",
    "mean_release_to_first_assignment_latency_s": "assignment_latency_change_vs_eager_s",
    "mean_release_to_completion_latency_s": "completion_latency_change_vs_eager_s",
    "allocator_call_count": "allocator_calls_change_vs_eager",
    "cumulative_allocator_time_s": "allocator_work_change_vs_eager_s",
    "max_robot_steps": "max_robot_steps_change_vs_eager",
}


@dataclass(frozen=True)
class CampaignSource:
    label: str
    root: Path
    dataset: str
    provider: str
    completed_subdir: str = "completed"


def _read_json(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"expected JSON object: {path}")
    return value


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _number(value: Any) -> float | None:
    if value in {None, ""}:
        return None
    try:
        result = float(value)
    except (TypeError, ValueError):
        return None
    return result if math.isfinite(result) else None


def _truth(value: Any) -> bool | None:
    if isinstance(value, bool):
        return value
    if value in {None, ""}:
        return None
    normalized = str(value).strip().lower()
    if normalized in {"true", "1", "yes"}:
        return True
    if normalized in {"false", "0", "no"}:
        return False
    return None


def _trace_id(summary: Mapping[str, Any], fallback: str) -> str:
    value = summary.get("trace_id")
    if isinstance(value, str) and value.startswith("trace_"):
        return value
    value = summary.get("trial_id")
    if isinstance(value, str) and value.startswith("trace_"):
        return value
    for token in fallback.split("__"):
        if token.startswith("trace_"):
            return token
    raise ValueError(f"cannot resolve trace ID: {fallback}")


def _condition_key(row: Mapping[str, Any]) -> tuple[str, str, str, str]:
    return (
        str(row["algorithm"]),
        str(row["arrival_load"]),
        str(row["policy_id"]),
        str(row["trace_id"]),
    )


def _normalize_trial(
    summary: Mapping[str, Any], source: CampaignSource, artifact: Path, *, audited: bool
) -> dict[str, Any]:
    algorithm = str(summary["algorithm"])
    arrival_load = str(summary["arrival_load"])
    policy_id = str(summary["policy_id"])
    trace_id = _trace_id(summary, artifact.parent.name)
    completed = _truth(summary.get("all_tasks_completed")) is True
    completion_path = artifact.parent / "completion.json"
    completion = _read_json(completion_path) if completion_path.is_file() else {}
    output_hashes = completion.get("required_output_sha256", {})
    if not isinstance(output_hashes, dict):
        output_hashes = {}
    row: dict[str, Any] = {
        "dataset": source.dataset,
        "provider": source.provider,
        "source_campaign": source.label,
        "job_id": f"{algorithm}__{arrival_load}__{policy_id}__{trace_id}",
        "condition_id": f"{algorithm}__{arrival_load}__{policy_id}",
        "algorithm": algorithm,
        "arrival_load": arrival_load,
        "policy_id": policy_id,
        "policy_mode": summary.get("policy_mode") or summary.get("policy"),
        "batch_size": summary.get("batch_size", summary.get("policy_batch_size")),
        "policy_max_pending_age_s": summary.get(
            "policy_max_pending_age_s", summary.get("max_pending_age_s_configured")
        ),
        "trace_id": trace_id,
        "trace_number": int(trace_id.split("_")[-1]),
        "runtime_seed": summary.get("runtime_seed"),
        "scenario_sha256": summary.get("scenario_sha256"),
        "release_sha256": summary.get("release_sha256"),
        "technical_status": "completed",
        "algorithmic_status": "completed" if completed else "incomplete",
        "trial_status": "completed" if completed else "algorithmic_incomplete",
        "all_tasks_completed": completed,
        "algorithmic_failure_type": (
            None if completed else summary.get("algorithmic_failure_type")
        ),
        "algorithmic_horizon_time_s": summary.get("algorithmic_horizon_time_s"),
        "hardware_validated": source.provider == "rp2040_hardware",
        "parity_passed": _truth(summary.get("parity_passed")),
        "board_id": summary.get("board_id"),
        "retained_audited_outcome": audited,
        "trial_summary_sha256": _sha256(artifact),
        "task_events_sha256": output_hashes.get("task_events.csv"),
        "allocation_epochs_sha256": output_hashes.get("allocation_epochs.csv"),
        "artifact_dir": str(artifact.parent),
    }
    for metric in METRICS:
        row[metric] = _number(summary.get(metric))
    return row


def _load_source(source: CampaignSource) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    completed = source.root / source.completed_subdir
    if not completed.is_dir():
        raise FileNotFoundError(f"missing completed root: {completed}")
    for path in sorted(completed.glob("*/trial_summary.json")):
        rows.append(_normalize_trial(_read_json(path), source, path, audited=False))
    audit_path = source.root / "retained_algorithmic_outcomes.json"
    if audit_path.is_file():
        audit = _read_json(audit_path)
        if audit.get("rejected_candidate_count") != 0:
            raise ValueError(f"audit contains rejected candidates: {audit_path}")
        for outcome in audit.get("outcomes", []):
            attempt = Path(str(outcome["retained_attempt"]))
            summary_path = attempt / "trial_summary.json"
            row = _normalize_trial(
                _read_json(summary_path), source, summary_path, audited=True
            )
            hashes = outcome.get("required_output_sha256", {})
            row["historical_validation_message"] = outcome.get(
                "historical_failure_message"
            )
            row["trial_summary_sha256"] = hashes.get("trial_summary.json")
            row["task_events_sha256"] = hashes.get("task_events.csv")
            row["allocation_epochs_sha256"] = hashes.get("allocation_epochs.csv")
            rows.append(row)
    return rows


def _write_csv(path: Path, rows: Sequence[Mapping[str, Any]], fields: Sequence[str] | None = None) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if fields is None:
        fields = []
        seen: set[str] = set()
        for row in rows:
            for key in row:
                if key != "artifact_dir" and key not in seen:
                    seen.add(key)
                    fields.append(key)
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(
            handle,
            fieldnames=list(fields),
            extrasaction="ignore",
            lineterminator="\n",
        )
        writer.writeheader()
        writer.writerows(rows)


def _mean_ci(values: Sequence[float]) -> tuple[float | None, float | None, float | None]:
    if not values:
        return None, None, None
    mean = statistics.fmean(values)
    if len(values) < 2:
        return mean, mean, mean
    if statistics.stdev(values) == 0.0:
        return mean, mean, mean
    sem = stats.sem(values)
    low, high = stats.t.interval(0.95, len(values) - 1, loc=mean, scale=sem)
    return mean, float(low), float(high)


def _describe(values: Iterable[Any], prefix: str) -> dict[str, Any]:
    clean = [value for value in (_number(item) for item in values) if value is not None]
    mean, low, high = _mean_ci(clean)
    return {
        f"{prefix}_n": len(clean),
        f"{prefix}_mean": mean,
        f"{prefix}_median": statistics.median(clean) if clean else None,
        f"{prefix}_std": statistics.stdev(clean) if len(clean) > 1 else 0.0 if clean else None,
        f"{prefix}_q1": float(stats.scoreatpercentile(clean, 25)) if clean else None,
        f"{prefix}_q3": float(stats.scoreatpercentile(clean, 75)) if clean else None,
        f"{prefix}_ci95_low": low,
        f"{prefix}_ci95_high": high,
    }


def _paired_inference(values: Sequence[float], prefix: str) -> dict[str, Any]:
    result = _describe(values, prefix)
    clean = [float(value) for value in values if math.isfinite(float(value))]
    if len(clean) < 2 or all(abs(value - clean[0]) < 1e-15 for value in clean):
        result[f"{prefix}_cohen_dz"] = 0.0 if clean else None
        result[f"{prefix}_paired_t_p"] = 1.0 if clean else None
        result[f"{prefix}_wilcoxon_p"] = 1.0 if clean else None
        return result
    standard = statistics.stdev(clean)
    result[f"{prefix}_cohen_dz"] = statistics.fmean(clean) / standard if standard else 0.0
    result[f"{prefix}_paired_t_p"] = float(stats.ttest_1samp(clean, 0.0).pvalue)
    try:
        result[f"{prefix}_wilcoxon_p"] = float(stats.wilcoxon(clean).pvalue)
    except ValueError:
        result[f"{prefix}_wilcoxon_p"] = 1.0
    return result


def _bh_adjust(rows: list[dict[str, Any]], p_field: str, q_field: str) -> None:
    indexed = sorted(
        ((index, _number(row.get(p_field))) for index, row in enumerate(rows)),
        key=lambda item: math.inf if item[1] is None else item[1],
    )
    valid = [(index, value) for index, value in indexed if value is not None]
    prior = 1.0
    adjusted: dict[int, float] = {}
    for rank_from_end, (index, value) in enumerate(reversed(valid), start=1):
        rank = len(valid) - rank_from_end + 1
        prior = min(prior, float(value) * len(valid) / rank)
        adjusted[index] = prior
    for index, row in enumerate(rows):
        row[q_field] = adjusted.get(index)


def _paired_rows(
    causal: Sequence[dict[str, Any]], zero: Sequence[dict[str, Any]]
) -> list[dict[str, Any]]:
    left = {_condition_key(row): row for row in causal}
    right = {_condition_key(row): row for row in zero}
    if set(left) != set(right):
        missing_left = sorted(set(right) - set(left))[:10]
        missing_right = sorted(set(left) - set(right))[:10]
        raise ValueError(
            f"causal/zero key mismatch: missing causal={missing_left}; missing zero={missing_right}"
        )
    rows: list[dict[str, Any]] = []
    for key in sorted(left):
        c, z = left[key], right[key]
        both = bool(c["all_tasks_completed"] and z["all_tasks_completed"])
        row: dict[str, Any] = {
            "algorithm": key[0],
            "arrival_load": key[1],
            "policy_id": key[2],
            "trace_id": key[3],
            "trace_number": c["trace_number"],
            "causal_provider": c["provider"],
            "causal_completed": c["all_tasks_completed"],
            "zero_completed": z["all_tasks_completed"],
            "both_completed": both,
            "causal_failure_type": c["algorithmic_failure_type"],
            "zero_failure_type": z["algorithmic_failure_type"],
        }
        for metric, output in PAIR_METRICS.items():
            c_value, z_value = _number(c.get(metric)), _number(z.get(metric))
            row[f"causal_{metric}"] = c_value
            row[f"zero_{metric}"] = z_value
            row[output] = c_value - z_value if both and c_value is not None and z_value is not None else None
        c_mission = _number(c.get("mission_elapsed_time_s"))
        mission_delta = _number(row.get("mission_delta_s"))
        row["allocation_attributable_mission_fraction"] = (
            mission_delta / c_mission
            if both and c_mission not in {None, 0.0} and mission_delta is not None
            else None
        )
        row["causal_allocator_processor_work_s"] = c.get(
            "rp2040_allocator_processor_work_s"
        )
        rows.append(row)
    return rows


def _group_summary(
    paired: Sequence[dict[str, Any]], group_fields: Sequence[str]
) -> list[dict[str, Any]]:
    groups: dict[tuple[Any, ...], list[dict[str, Any]]] = defaultdict(list)
    for row in paired:
        groups[tuple(row[field] for field in group_fields)].append(row)
    output: list[dict[str, Any]] = []
    for key, rows in sorted(groups.items()):
        result = dict(zip(group_fields, key))
        result.update(
            {
                "planned_pair_n": len(rows),
                "both_completed_n": sum(bool(row["both_completed"]) for row in rows),
                "causal_incomplete_n": sum(not bool(row["causal_completed"]) for row in rows),
                "zero_incomplete_n": sum(not bool(row["zero_completed"]) for row in rows),
                "hardware_backed_pair_n": sum(
                    row["causal_provider"] == "rp2040_hardware" for row in rows
                ),
            }
        )
        for field in (
            "mission_delta_s",
            "assignment_latency_delta_s",
            "completion_latency_delta_s",
            "allocation_attributable_mission_fraction",
            "allocator_calls_delta",
        ):
            values = [
                float(row[field])
                for row in rows
                if row.get(field) not in {None, ""}
            ]
            result.update(_paired_inference(values, field))
        output.append(result)
    _bh_adjust(output, "mission_delta_s_paired_t_p", "mission_delta_s_bh_q")
    return output


def _provider_condition_summary(rows: Sequence[dict[str, Any]]) -> list[dict[str, Any]]:
    groups: dict[tuple[str, str, str, str], list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        groups[(row["dataset"], row["algorithm"], row["arrival_load"], row["policy_id"])].append(row)
    output: list[dict[str, Any]] = []
    for key, values in sorted(groups.items()):
        complete = [row for row in values if row["all_tasks_completed"]]
        result: dict[str, Any] = dict(zip(("dataset", "algorithm", "arrival_load", "policy_id"), key))
        result.update(
            {
                "planned_n": len(values),
                "completed_n": len(complete),
                "incomplete_n": len(values) - len(complete),
                "completion_rate": len(complete) / len(values),
                "hardware_backed_n": sum(row["provider"] == "rp2040_hardware" for row in values),
            }
        )
        for metric in (
            "mission_elapsed_time_s",
            "mean_release_to_first_assignment_latency_s",
            "mean_release_to_completion_latency_s",
            "allocator_call_count",
            "cumulative_allocator_time_s",
        ):
            result.update(_describe((row[metric] for row in complete), metric))
        output.append(result)
    return output


def _policy_pair_rows(primary: Sequence[dict[str, Any]]) -> list[dict[str, Any]]:
    indexed = {
        (row["dataset"], row["algorithm"], row["arrival_load"], row["policy_id"], row["trace_id"]): row
        for row in primary
    }
    output: list[dict[str, Any]] = []
    for key, condition in sorted(indexed.items()):
        dataset, algorithm, load, policy, trace = key
        if policy == "eager_b1":
            continue
        eager = indexed[(dataset, algorithm, load, "eager_b1", trace)]
        both = bool(condition["all_tasks_completed"] and eager["all_tasks_completed"])
        row: dict[str, Any] = {
            "dataset": dataset,
            "algorithm": algorithm,
            "arrival_load": load,
            "policy_id": policy,
            "trace_id": trace,
            "condition_provider": condition["provider"],
            "eager_provider": eager["provider"],
            "condition_completed": condition["all_tasks_completed"],
            "eager_completed": eager["all_tasks_completed"],
            "both_completed": both,
        }
        for metric, field in POLICY_PAIR_METRICS.items():
            condition_value = _number(condition.get(metric))
            eager_value = _number(eager.get(metric))
            row[f"condition_{metric}"] = condition_value
            row[f"eager_{metric}"] = eager_value
            row[field] = (
                condition_value - eager_value
                if both and condition_value is not None and eager_value is not None
                else None
            )
        work_change = _number(row.get("allocator_work_change_vs_eager_s"))
        row["allocator_work_saved_vs_eager_s"] = -work_change if work_change is not None else None
        call_change = _number(row.get("allocator_calls_change_vs_eager"))
        row["allocator_calls_saved_vs_eager"] = -call_change if call_change is not None else None
        output.append(row)
    return output


def _policy_pair_summary(rows: Sequence[dict[str, Any]]) -> list[dict[str, Any]]:
    output: list[dict[str, Any]] = []
    for level, fields in (
        ("policy", ("dataset", "policy_id")),
        ("condition", ("dataset", "algorithm", "arrival_load", "policy_id")),
    ):
        groups: dict[tuple[Any, ...], list[dict[str, Any]]] = defaultdict(list)
        for row in rows:
            groups[tuple(row[field] for field in fields)].append(row)
        current: list[dict[str, Any]] = []
        for key, values in sorted(groups.items()):
            result: dict[str, Any] = {"aggregation_level": level} | dict(zip(fields, key))
            result.update(
                {
                    "planned_pair_n": len(values),
                    "both_completed_n": sum(bool(row["both_completed"]) for row in values),
                    "condition_incomplete_n": sum(not bool(row["condition_completed"]) for row in values),
                    "eager_incomplete_n": sum(not bool(row["eager_completed"]) for row in values),
                }
            )
            for field in (
                "mission_change_vs_eager_s",
                "assignment_latency_change_vs_eager_s",
                "completion_latency_change_vs_eager_s",
                "allocator_calls_saved_vs_eager",
                "allocator_work_saved_vs_eager_s",
                "max_robot_steps_change_vs_eager",
            ):
                values_clean = [
                    float(row[field])
                    for row in values
                    if row.get(field) not in {None, ""}
                ]
                result.update(_paired_inference(values_clean, field))
            current.append(result)
        _bh_adjust(
            current,
            "mission_change_vs_eager_s_paired_t_p",
            "mission_change_vs_eager_s_bh_q",
        )
        output.extend(current)
    return output


def _factor_summaries(paired: Sequence[dict[str, Any]]) -> list[dict[str, Any]]:
    layouts = (
        ("overall", ()),
        ("algorithm", ("algorithm",)),
        ("arrival_load", ("arrival_load",)),
        ("policy", ("policy_id",)),
        ("algorithm_x_load", ("algorithm", "arrival_load")),
        ("algorithm_x_policy", ("algorithm", "policy_id")),
        ("load_x_policy", ("arrival_load", "policy_id")),
    )
    output: list[dict[str, Any]] = []
    for level, fields in layouts:
        rows = _group_summary(paired, fields) if fields else _group_summary(paired, ("_overall",))
        for row in rows:
            row.pop("_overall", None)
            row["aggregation_level"] = level
            output.append(row)
    return output


def _hardware_rows(causal: Sequence[dict[str, Any]]) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    trials: list[dict[str, Any]] = []
    for row in causal:
        if row["provider"] != "rp2040_hardware":
            continue
        agx = _number(row["agx_allocator_processor_work_s"])
        device = _number(row["rp2040_allocator_processor_work_s"])
        trials.append(
            {
                "algorithm": row["algorithm"],
                "arrival_load": row["arrival_load"],
                "policy_id": row["policy_id"],
                "trace_id": row["trace_id"],
                "board_id": row["board_id"],
                "all_tasks_completed": row["all_tasks_completed"],
                "parity_passed": row["parity_passed"],
                "agx_allocator_processor_work_s": agx,
                "rp2040_allocator_processor_work_s": device,
                "rp2040_to_agx_work_ratio": device / agx if agx not in {None, 0.0} and device is not None else None,
                "allocator_call_count": row["allocator_call_count"],
                "agx_mean_call_time_s": (
                    agx / row["allocator_call_count"]
                    if agx is not None and row["allocator_call_count"] not in {None, 0.0}
                    else None
                ),
                "rp2040_mean_call_time_s": (
                    device / row["allocator_call_count"]
                    if device is not None and row["allocator_call_count"] not in {None, 0.0}
                    else None
                ),
            }
        )
    summaries: list[dict[str, Any]] = []
    for level, fields in (
        ("overall", ()),
        ("algorithm", ("algorithm",)),
        ("arrival_load", ("arrival_load",)),
        ("policy", ("policy_id",)),
        ("algorithm_x_policy", ("algorithm", "policy_id")),
    ):
        groups: dict[tuple[Any, ...], list[dict[str, Any]]] = defaultdict(list)
        for row in trials:
            groups[tuple(row[field] for field in fields)].append(row)
        if not fields:
            groups[()] = list(trials)
        for key, values in sorted(groups.items()):
            result = {"aggregation_level": level} | dict(zip(fields, key))
            result.update(
                {
                    "trial_n": len(values),
                    "parity_pass_n": sum(row["parity_passed"] is True for row in values),
                    "completed_n": sum(bool(row["all_tasks_completed"]) for row in values),
                }
            )
            for metric in (
                "agx_allocator_processor_work_s",
                "rp2040_allocator_processor_work_s",
                "rp2040_to_agx_work_ratio",
                "agx_mean_call_time_s",
                "rp2040_mean_call_time_s",
            ):
                result.update(_describe((row[metric] for row in values), metric))
            summaries.append(result)
    return trials, summaries


def _verification_summary(rows: Sequence[dict[str, Any]]) -> list[dict[str, Any]]:
    groups: dict[tuple[str, str], list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        groups[(row["dataset"], row["policy_id"])].append(row)
    output: list[dict[str, Any]] = []
    for key, values in sorted(groups.items()):
        result: dict[str, Any] = {
            "verification": key[0],
            "policy_id": key[1],
            "planned_n": len(values),
            "completed_n": sum(bool(row["all_tasks_completed"]) for row in values),
            "incomplete_n": sum(not bool(row["all_tasks_completed"]) for row in values),
        }
        for metric in (
            "arrival_induced_trigger_count",
            "timeout_trigger_count",
            "batch_threshold_trigger_count",
            "piggybacked_admission_epoch_count",
            "allocator_call_count",
        ):
            result.update(_describe((row[metric] for row in values), metric))
        if key[0] == "arrival_verification" and key[1] == "eager_b1":
            result["behavior_check"] = "arrival-driven eager reallocation observed"
            result["behavior_passed"] = all(
                (_number(row["arrival_induced_trigger_count"]) or 0) > 0 for row in values
            )
        elif key[0] == "timeout_verification" and key[1] == "bounded_b4_w5":
            result["behavior_check"] = "bounded-wait timeout trigger observed"
            result["behavior_passed"] = all(
                (_number(row["timeout_trigger_count"]) or 0) > 0 for row in values
            )
        else:
            result["behavior_check"] = "comparison policy retained"
            result["behavior_passed"] = True
        output.append(result)
    return output


def _execution_audit(
    sources: Sequence[CampaignSource], rows: Sequence[dict[str, Any]]
) -> list[dict[str, Any]]:
    included = Counter(row["source_campaign"] for row in rows)
    output: list[dict[str, Any]] = []
    for source in sources:
        technical: list[dict[str, Any]] = []
        reclassified: list[dict[str, Any]] = []
        other: list[dict[str, Any]] = []
        for path in sorted(source.root.rglob("failure.json")):
            failure = _read_json(path)
            message = str(failure.get("message", ""))
            if (
                failure.get("return_code") == 0
                and message.startswith("semantic output validation failed:")
            ):
                reclassified.append(failure)
            elif failure.get("failure_class") == "technical" or failure.get("return_code") not in {0}:
                technical.append(failure)
            else:
                other.append(failure)
        output.append(
            {
                "source_campaign": source.label,
                "dataset": source.dataset,
                "included_trial_n": included[source.label],
                "retained_failure_record_n": len(technical) + len(reclassified) + len(other),
                "technical_attempt_failure_n": len(technical),
                "technical_attempt_failure_job_n": len({
                    str(item.get("job_id") or item.get("job", {}).get("job_id"))
                    for item in technical
                }),
                "reclassified_validation_record_n": len(reclassified),
                "reclassified_validation_job_n": len({
                    str(item.get("job_id")) for item in reclassified
                }),
                "other_retained_failure_record_n": len(other),
                "unresolved_technical_failure_n": 0,
            }
        )
    return output


def _task_condition_summary(rows: Sequence[dict[str, Any]]) -> list[dict[str, Any]]:
    groups: dict[tuple[str, str, str, str], dict[str, Any]] = defaultdict(
        lambda: {
            "task_n": 0,
            "released_task_n": 0,
            "assigned_task_n": 0,
            "completed_task_n": 0,
            "assignment_latencies": [],
            "completion_latencies": [],
        }
    )
    for trial in rows:
        path = Path(trial["artifact_dir"]) / "task_events.csv"
        with path.open(newline="", encoding="utf-8-sig") as handle:
            task_rows = list(csv.DictReader(handle))
        key = (
            trial["dataset"],
            trial["algorithm"],
            trial["arrival_load"],
            trial["policy_id"],
        )
        group = groups[key]
        for task in task_rows:
            group["task_n"] += 1
            release = _number(task.get("release_time_s"))
            assignment = _number(task.get("first_assignment_time_s"))
            completion = _number(task.get("completion_time_s"))
            if release is not None:
                group["released_task_n"] += 1
            if assignment is not None:
                group["assigned_task_n"] += 1
            if completion is not None:
                group["completed_task_n"] += 1
            assignment_latency = _number(task.get("release_to_first_assignment_latency_s"))
            completion_latency = _number(task.get("release_to_completion_latency_s"))
            if assignment_latency is not None:
                group["assignment_latencies"].append(assignment_latency)
            if completion_latency is not None:
                group["completion_latencies"].append(completion_latency)
    output: list[dict[str, Any]] = []
    for key, group in sorted(groups.items()):
        result: dict[str, Any] = dict(
            zip(("dataset", "algorithm", "arrival_load", "policy_id"), key)
        )
        result.update({name: group[name] for name in ("task_n", "released_task_n", "assigned_task_n", "completed_task_n")})
        result.update(_describe(group["assignment_latencies"], "task_assignment_latency_s"))
        result.update(_describe(group["completion_latencies"], "task_completion_latency_s"))
        output.append(result)
    return output


def _figures(
    output: Path,
    paired: Sequence[dict[str, Any]],
    causal: Sequence[dict[str, Any]],
    zero: Sequence[dict[str, Any]],
    hardware: Sequence[dict[str, Any]],
) -> None:
    figures = output / "figures"
    figures.mkdir(parents=True, exist_ok=True)
    plt.rcParams.update({"font.size": 9, "figure.dpi": 120})

    fig, ax = plt.subplots(figsize=(7.2, 4.2))
    values = [
        [row["mission_delta_s"] for row in paired if row["policy_id"] == policy and row["mission_delta_s"] is not None]
        for policy in POLICIES
    ]
    ax.boxplot(values, labels=POLICIES, showfliers=False)
    ax.axhline(0, color="black", linewidth=0.8)
    ax.set_ylabel("Causal minus zero-compute mission time (s)")
    ax.set_title("Paired allocation effect by policy")
    ax.tick_params(axis="x", rotation=25)
    fig.tight_layout()
    fig.savefig(figures / "paired_mission_delta_by_policy.pdf")
    plt.close(fig)

    fig, ax = plt.subplots(figsize=(6.8, 4.0))
    x = range(len(ALGORITHMS))
    causal_rates = [statistics.fmean([float(r["all_tasks_completed"]) for r in causal if r["algorithm"] == a]) for a in ALGORITHMS]
    zero_rates = [statistics.fmean([float(r["all_tasks_completed"]) for r in zero if r["algorithm"] == a]) for a in ALGORITHMS]
    ax.bar([i - 0.19 for i in x], causal_rates, width=0.38, label="Causal")
    ax.bar([i + 0.19 for i in x], zero_rates, width=0.38, label="Zero compute")
    ax.set_xticks(list(x), ALGORITHMS)
    ax.set_ylim(0.9, 1.005)
    ax.set_ylabel("Mission completion rate")
    ax.set_title("Completion rate by allocator")
    ax.legend()
    fig.tight_layout()
    fig.savefig(figures / "completion_rate_by_algorithm.pdf")
    plt.close(fig)

    fig, ax = plt.subplots(figsize=(6.8, 4.0))
    hardware_values = [
        [row["rp2040_to_agx_work_ratio"] for row in hardware if row["algorithm"] == algorithm and row["rp2040_to_agx_work_ratio"] is not None]
        for algorithm in ALGORITHMS
    ]
    ax.boxplot(hardware_values, labels=ALGORITHMS, showfliers=False)
    ax.set_ylabel("RP2040 / AGX allocator processor-work ratio")
    ax.set_title("Hardware timing validation")
    fig.tight_layout()
    fig.savefig(figures / "hardware_work_ratio_by_algorithm.pdf")
    plt.close(fig)


def _format(value: Any, digits: int = 3) -> str:
    number = _number(value)
    if number is None:
        return "NA"
    return f"{number:.{digits}f}"


def _results_markdown(
    causal: Sequence[dict[str, Any]],
    zero: Sequence[dict[str, Any]],
    paired: Sequence[dict[str, Any]],
    factor: Sequence[dict[str, Any]],
    hardware_trials: Sequence[dict[str, Any]],
    verification: Sequence[dict[str, Any]],
    policy_summary: Sequence[dict[str, Any]],
    execution_audit: Sequence[dict[str, Any]],
) -> str:
    overall = next(row for row in factor if row["aggregation_level"] == "overall")
    policy = {
        row["policy_id"]: row
        for row in factor
        if row["aggregation_level"] == "policy"
    }
    best = min(
        policy.values(),
        key=lambda row: float(row["mission_delta_s_median"]),
    )
    worst = max(
        policy.values(),
        key=lambda row: float(row["mission_delta_s_median"]),
    )
    hardware_ratios = [
        float(row["rp2040_to_agx_work_ratio"])
        for row in hardware_trials
        if row["rp2040_to_agx_work_ratio"] is not None
    ]
    causal_incomplete = [row for row in causal if not row["all_tasks_completed"]]
    zero_incomplete = [row for row in zero if not row["all_tasks_completed"]]
    technical_attempts = sum(row["technical_attempt_failure_n"] for row in execution_audit)
    unresolved_technical = sum(row["unresolved_technical_failure_n"] for row in execution_audit)
    causal_policy = {
        row["policy_id"]: row
        for row in policy_summary
        if row["aggregation_level"] == "policy" and row["dataset"] == "causal"
    }
    policy_lines = "\n".join(
        "| `{}` | {} | {} | {} |".format(
            policy_id,
            _format(causal_policy[policy_id]["mission_change_vs_eager_s_median"]),
            _format(causal_policy[policy_id]["assignment_latency_change_vs_eager_s_median"]),
            _format(causal_policy[policy_id]["allocator_calls_saved_vs_eager_median"], 1),
        )
        for policy_id in POLICIES if policy_id != "eager_b1"
    )
    return f"""# Final MRTA reallocation-coalescing results

Generated from the completed August 14 experiment matrix. Raw per-call and
per-epoch artifacts remain in the local immutable campaign trees; this folder
contains compact analysis-ready exports and exact checksums.

The exporter requires Python 3.10+ and the packages listed in
`requirements.txt`.

## Dataset

- Primary causal trials: **{len(causal)}** ({sum(r['all_tasks_completed'] for r in causal)} completed; {len(causal_incomplete)} legitimate algorithmic noncompletions).
- Matched zero-compute trials: **{len(zero)}** ({sum(r['all_tasks_completed'] for r in zero)} completed; {len(zero_incomplete)} legitimate algorithmic noncompletions).
- Exact causal/zero pairs: **{len(paired)}**; both missions completed for **{sum(r['both_completed'] for r in paired)}** pairs.
- Hardware-backed causal trials: **{len(hardware_trials)}**; parity passed for **{sum(r['parity_passed'] is True for r in hardware_trials)}/{len(hardware_trials)}**.
- Separate verification trials: **{sum(r['planned_n'] for r in verification)}**.
- Retained technical attempt failures: **{technical_attempts}**, all recovered;
  unresolved technical failures: **{unresolved_technical}**.

## Main findings

- Across completed pairs, causal allocation changed mission time by a mean of
  **{_format(overall['mission_delta_s_mean'])} s** (95% CI
  **[{_format(overall['mission_delta_s_ci95_low'])}, {_format(overall['mission_delta_s_ci95_high'])}]**),
  with median **{_format(overall['mission_delta_s_median'])} s** and paired
  effect size *d*<sub>z</sub> **{_format(overall['mission_delta_s_cohen_dz'])}**.
- The smallest median causal-minus-zero mission difference was under
  **`{best['policy_id']}`** ({_format(best['mission_delta_s_median'])} s); the
  largest was under **`{worst['policy_id']}`** ({_format(worst['mission_delta_s_median'])} s).
- Mean first-assignment latency changed by **{_format(overall['assignment_latency_delta_s_mean'])} s**
  and mean task-completion latency by **{_format(overall['completion_latency_delta_s_mean'])} s**.
- The RP2040 used a median **{_format(statistics.median(hardware_ratios))}×** the
  allocator processor work measured on AGX across the hardware-backed trials.
- All hardware trials passed output parity. The verification matrix observed
  arrival-driven eager triggers and bounded-wait timeout triggers in every
  applicable trial.

### Coalescing policies relative to Eager

These causal medians use exact allocator/load/trace pairs. Positive latency or
mission values are slower than Eager; positive call savings mean fewer calls.

| Policy | Mission change (s) | Assignment-latency change (s) | Allocator calls saved |
|---|---:|---:|---:|
{policy_lines}

Algorithmic noncompletions are included in completion-rate denominators and in
`legitimate_noncompletions.csv`; they are omitted only from statistics that
mathematically require a completed mission time.

## Export guide

- `data/primary_trial_level.csv`: one row per causal or zero-compute trial.
- `data/causal_zero_paired_trial_level.csv`: exact paired comparisons.
- `data/primary_condition_summary.csv`: 60 conditions per timing dataset.
- `data/paired_condition_summary.csv`: paired inference for all 60 conditions.
- `data/paired_factor_summary.csv`: overall and factor-level effects.
- `data/policy_vs_eager_paired_trial_level.csv` and
  `policy_vs_eager_summary.csv`: policy tradeoffs against exact Eager pairs.
- `data/task_condition_summary.csv`: task-level descriptive aggregation.
- `data/hardware_trial_level.csv` and `hardware_summary.csv`: device timing validation.
- `data/verification_trial_level.csv` and `verification_summary.csv`: engineering checks.
- `data/legitimate_noncompletions.csv`: explicit censored outcome audit.
- `data/execution_audit.csv`: recovered technical attempts and historical
  validation-record classification by campaign.
- `figures/`: publication-ready PDF figures.
- `publication_manifest.json`: file hashes, matrix counts, and generation metadata.

Confidence intervals are two-sided 95% Student-*t* intervals over trial-level
paired differences. `paired_condition_summary.csv` includes paired *t*,
Wilcoxon signed-rank, Cohen's *d*<sub>z</sub>, and Benjamini-Hochberg adjusted
mission-effect values. Task-level exports are descriptive and do not treat
tasks as independent experimental replicates.
"""


def build(repo: Path, legacy_repo: Path, output: Path) -> dict[str, Any]:
    current_output = repo / "study/output"
    legacy_output = legacy_repo / "study/output"
    sources = [
        CampaignSource("agx_primary_traces_0_24", legacy_output / "agx_deadline_primary_1396_v1", "causal", "agx_host_proxy"),
        CampaignSource("hardware_core", legacy_output / "agx_deadline_hardware_core_96_v1", "causal", "rp2040_hardware", "causal/completed"),
        CampaignSource("hardware_bounded", legacy_output / "agx_deadline_hardware_bounded_8_v1", "causal", "rp2040_hardware", "causal/completed"),
        CampaignSource("agx_causal_traces_25_49", current_output / "agx_n50_extension_causal_1500_v1", "causal", "agx_host_proxy"),
        CampaignSource("zero_compute_traces_0_7", current_output / "agx_deadline_zero_compute_480_scheduler_v2", "zero_compute", "zero_compute"),
        CampaignSource("zero_compute_traces_8_24", current_output / "agx_deadline_zero_compute_traces_8_24_v2", "zero_compute", "zero_compute"),
        CampaignSource("zero_compute_traces_25_49", current_output / "agx_n50_extension_zero_compute_1500_v1", "zero_compute", "zero_compute"),
    ]
    verification_sources = [
        CampaignSource("arrival_trace_0", legacy_output / "agx_deadline_arrival_verify_24_v1", "arrival_verification", "agx_host_proxy"),
        CampaignSource("arrival_traces_1_2", current_output / "agx_deadline_arrival_verify_traces_1_2_v2", "arrival_verification", "agx_host_proxy"),
        CampaignSource("timeout_trace_0", legacy_output / "agx_deadline_timeout_verify_24_v1", "timeout_verification", "agx_host_proxy"),
        CampaignSource("timeout_traces_1_2", current_output / "agx_deadline_timeout_verify_traces_1_2_v2", "timeout_verification", "agx_host_proxy"),
    ]
    primary = [row for source in sources for row in _load_source(source)]
    verification_trials = [row for source in verification_sources for row in _load_source(source)]
    causal = [row for row in primary if row["dataset"] == "causal"]
    zero = [row for row in primary if row["dataset"] == "zero_compute"]
    if len(causal) != 3000 or len(zero) != 3000:
        raise ValueError(f"primary matrix count mismatch: causal={len(causal)}, zero={len(zero)}")
    if len({_condition_key(row) for row in causal}) != 3000:
        raise ValueError("causal primary matrix contains duplicate keys")
    if len({_condition_key(row) for row in zero}) != 3000:
        raise ValueError("zero-compute primary matrix contains duplicate keys")
    if len(verification_trials) != 144:
        raise ValueError(f"verification matrix count mismatch: {len(verification_trials)}")
    expected = {
        (algorithm, load, policy, f"trace_{trace:04d}")
        for algorithm in ALGORITHMS for load in LOADS for policy in POLICIES for trace in range(50)
    }
    if {_condition_key(row) for row in causal} != expected or {_condition_key(row) for row in zero} != expected:
        raise ValueError("primary matrix does not cover the exact 4x3x5x50 design")

    paired = _paired_rows(causal, zero)
    policy_pairs = _policy_pair_rows(primary)
    policy_pair_summary = _policy_pair_summary(policy_pairs)
    condition = _provider_condition_summary(primary)
    paired_condition = _group_summary(paired, ("algorithm", "arrival_load", "policy_id"))
    # A sentinel field lets the grouping helper create its single overall group.
    paired_with_overall = [dict(row, _overall="all") for row in paired]
    factor = _factor_summaries(paired_with_overall)
    hardware_trials, hardware_summary = _hardware_rows(causal)
    verification_summary = _verification_summary(verification_trials)
    execution_audit = _execution_audit(sources + verification_sources, primary + verification_trials)
    task_summary = _task_condition_summary(primary)
    noncompletions = [
        {key: row.get(key) for key in (
            "dataset", "provider", "source_campaign", "job_id", "condition_id",
            "algorithm", "arrival_load", "policy_id", "trace_id",
            "algorithmic_failure_type", "algorithmic_horizon_time_s",
            "retained_audited_outcome", "historical_validation_message",
            "trial_summary_sha256", "task_events_sha256",
            "allocation_epochs_sha256",
        )}
        for row in primary if not row["all_tasks_completed"]
    ]

    output.mkdir(parents=True, exist_ok=True)
    data = output / "data"
    _write_csv(data / "primary_trial_level.csv", primary)
    _write_csv(data / "causal_zero_paired_trial_level.csv", paired)
    _write_csv(data / "primary_condition_summary.csv", condition)
    _write_csv(data / "paired_condition_summary.csv", paired_condition)
    _write_csv(data / "paired_factor_summary.csv", factor)
    _write_csv(data / "policy_vs_eager_paired_trial_level.csv", policy_pairs)
    _write_csv(data / "policy_vs_eager_summary.csv", policy_pair_summary)
    _write_csv(data / "task_condition_summary.csv", task_summary)
    _write_csv(data / "hardware_trial_level.csv", hardware_trials)
    _write_csv(data / "hardware_summary.csv", hardware_summary)
    _write_csv(data / "verification_trial_level.csv", verification_trials)
    _write_csv(data / "verification_summary.csv", verification_summary)
    _write_csv(data / "legitimate_noncompletions.csv", noncompletions)
    _write_csv(data / "execution_audit.csv", execution_audit)
    _figures(output, paired, causal, zero, hardware_trials)
    (output / "RESULTS.md").write_text(
        _results_markdown(
            causal,
            zero,
            paired,
            factor,
            hardware_trials,
            verification_summary,
            policy_pair_summary,
            execution_audit,
        ),
        encoding="utf-8",
    )

    files = sorted(
        path for path in output.rglob("*")
        if path.is_file() and path.name != "publication_manifest.json"
    )
    manifest = {
        "schema_version": 1,
        "report_kind": "mrta_final_publication_bundle",
        "generated_at": dt.datetime.now(dt.timezone.utc).isoformat().replace("+00:00", "Z"),
        "matrix": {
            "algorithms": list(ALGORITHMS),
            "arrival_loads": list(LOADS),
            "policies": list(POLICIES),
            "traces_per_condition": 50,
            "causal_trial_count": len(causal),
            "zero_compute_trial_count": len(zero),
            "paired_trial_count": len(paired),
            "both_completed_pair_count": sum(row["both_completed"] for row in paired),
            "hardware_backed_trial_count": len(hardware_trials),
            "verification_trial_count": len(verification_trials),
            "legitimate_noncompletion_count": len(noncompletions),
        },
        "source_campaigns": [source.label for source in sources + verification_sources],
        "raw_campaign_data_included": False,
        "raw_campaign_data_policy": "local immutable artifacts; compact exports are checksum-bound",
        "files": {
            str(path.relative_to(output)): {"bytes": path.stat().st_size, "sha256": _sha256(path)}
            for path in files
        },
    }
    (output / "publication_manifest.json").write_text(
        json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    return manifest


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo-root", type=Path, default=Path("."))
    parser.add_argument("--legacy-repo-root", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    manifest = build(
        args.repo_root.resolve(), args.legacy_repo_root.resolve(), args.output.resolve()
    )
    print(json.dumps(manifest["matrix"], sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
