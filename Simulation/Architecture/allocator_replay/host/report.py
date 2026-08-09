from __future__ import annotations

import csv
import json
import math
import statistics
from collections import defaultdict
from pathlib import Path
from typing import Any, Iterable


TIME_METRICS = {
    "allocator": "allocator_time_us",
    "filter": "candidate_filter_time_us",
    "allocator_exclusive": "allocator_exclusive_time_us",
}


def _read_attempts(campaign_root: Path) -> list[dict[str, Any]]:
    deduplicated: dict[str, dict[str, Any]] = {}
    for path in sorted((campaign_root / "journals").glob("*.jsonl")):
        with path.open(encoding="utf-8") as handle:
            for line in handle:
                if line.strip():
                    record = json.loads(line)
                    deduplicated[record["attempt_id"]] = record
    return sorted(
        deduplicated.values(),
        key=lambda item: (
            item.get("condition_id", ""),
            int(item.get("trial_id", -1)),
            item.get("robot_id", ""),
            int(item.get("call_index", -1)),
            int(item.get("repetition_id", -1)),
            item.get("attempt_id", ""),
        ),
    )


def _atomic_csv(path: Path, fields: list[str], rows: Iterable[dict[str, Any]]) -> None:
    temporary = path.with_suffix(path.suffix + ".tmp")
    with temporary.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)
    temporary.replace(path)


def _p95(values: list[float]) -> float:
    ordered = sorted(values)
    if not ordered:
        return math.nan
    if len(ordered) == 1:
        return ordered[0]
    position = 0.95 * (len(ordered) - 1)
    lower = int(math.floor(position))
    upper = int(math.ceil(position))
    fraction = position - lower
    return ordered[lower] * (1.0 - fraction) + ordered[upper] * fraction


def _stats(rows: list[dict[str, Any]]) -> dict[str, Any]:
    output: dict[str, Any] = {
        "call_count": len(rows),
        "candidate_filter_call_count": sum(
            int(row.get("candidate_filter_calls") or 0) for row in rows
        ),
    }
    for label, field in TIME_METRICS.items():
        values = [float(row[field]) for row in rows if row.get(field) is not None]
        output.update(
            {
                f"{label}_total_us": sum(values),
                f"{label}_mean_us": statistics.fmean(values) if values else "",
                f"{label}_median_us": statistics.median(values) if values else "",
                f"{label}_p95_us": _p95(values) if values else "",
                f"{label}_maximum_us": max(values) if values else "",
            }
        )
    return output


def _metric_fields() -> list[str]:
    fields = ["call_count", "candidate_filter_call_count"]
    for label in TIME_METRICS:
        fields.extend(
            [
                f"{label}_total_us",
                f"{label}_mean_us",
                f"{label}_median_us",
                f"{label}_p95_us",
                f"{label}_maximum_us",
            ]
        )
    return fields


def _group(
    rows: list[dict[str, Any]],
    keys: tuple[str, ...],
) -> list[dict[str, Any]]:
    grouped: dict[tuple[Any, ...], list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        grouped[tuple(row.get(key) for key in keys)].append(row)
    output: list[dict[str, Any]] = []
    for group_key, group_rows in sorted(grouped.items()):
        first = group_rows[0]
        record = {key: value for key, value in zip(keys, group_key)}
        for context in (
            "mission",
            "algorithm",
            "top_k_level",
            "top_k_rate",
            "top_k_cells",
        ):
            if context not in record:
                record[context] = first.get(context)
        record["device_ids"] = ";".join(
            sorted({str(row.get("device_id", "")) for row in group_rows})
        )
        record.update(_stats(group_rows))
        output.append(record)
    return output


def rebuild_reports(campaign_root: Path) -> dict[str, object]:
    campaign_root = campaign_root.resolve()
    schedule = json.loads(
        (campaign_root / "schedule.json").read_text(encoding="utf-8")
    )
    attempts = _read_attempts(campaign_root)
    output_root = campaign_root / "reports"
    output_root.mkdir(parents=True, exist_ok=True)

    raw_fields = [
        "attempt_id",
        "condition_id",
        "fixture_id",
        "fixture_sha256",
        "repetition_id",
        "mission",
        "algorithm",
        "top_k_level",
        "top_k_rate",
        "top_k_cells",
        "trial_id",
        "robot_id",
        "call_index",
        "call_class",
        "device_id",
        "port",
        "build_id",
        "frequency_hz",
        "status",
        "outcome",
        "failure_type",
        "timing_attempt_counted",
        "accepted",
        "allocator_time_us",
        "candidate_filter_time_us",
        "allocator_exclusive_time_us",
        "candidate_filter_calls",
        "heap_free_before",
        "heap_free_after",
        "goal_match",
        "state_match",
        "messages_match",
        "error",
        "host_started_at",
        "host_finished_at",
        "journaled_at",
    ]
    _atomic_csv(output_root / "raw_attempts.csv", raw_fields, attempts)

    feasible_ids = {
        condition_id
        for condition_id, entry in schedule["conditions"].items()
        if entry.get("classification") == "hardware_feasible_30s"
    }
    accepted = [
        row
        for row in attempts
        if row.get("accepted")
        and row.get("status") == "completed"
        and row.get("condition_id") in feasible_ids
    ]
    accepted_attempt_ids = {row["attempt_id"] for row in accepted}
    robot_keys = ("condition_id", "trial_id", "robot_id")
    system_keys = ("condition_id", "trial_id")
    robot_rows = _group(accepted, robot_keys)
    system_rows = _group(accepted, system_keys)
    context = [
        "mission",
        "algorithm",
        "top_k_level",
        "top_k_rate",
        "top_k_cells",
        "device_ids",
    ]
    metrics = _metric_fields()
    _atomic_csv(
        output_root / "robot_trial_metrics.csv",
        [*robot_keys, *context, *metrics],
        robot_rows,
    )
    _atomic_csv(
        output_root / "system_trial_metrics.csv",
        [*system_keys, *context, *metrics],
        system_rows,
    )

    attempts_by_condition: dict[str, list[dict[str, Any]]] = defaultdict(list)
    accepted_by_condition: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in attempts:
        attempts_by_condition[str(row.get("condition_id", ""))].append(row)
        if row.get("attempt_id") in accepted_attempt_ids:
            accepted_by_condition[str(row["condition_id"])].append(row)
    condition_rows: list[dict[str, Any]] = []
    for condition_id, entry in sorted(schedule["conditions"].items()):
        condition_attempts = attempts_by_condition[condition_id]
        row: dict[str, Any] = {
            "condition_id": condition_id,
            "mission": entry["mission"],
            "algorithm": entry["algorithm"],
            "top_k_level": entry["top_k_level"],
            "top_k_rate": entry["top_k_rate"],
            "top_k_cells": entry["top_k_cells"],
            "trial_start_index": entry.get("trial_start_index", 0),
            "trial_count": entry.get("trial_count", ""),
            "status": entry["status"],
            "classification": entry.get("classification", ""),
            "representative_metrics": (
                entry.get("classification") == "hardware_feasible_30s"
            ),
            "fixture_total": entry["fixture_total"],
            "fixtures_completed": entry["fixtures_completed"],
            "attempt_count": len(condition_attempts),
            "timing_attempt_count": sum(
                bool(item.get("timing_attempt_counted"))
                for item in condition_attempts
            ),
            "transport_error_count": sum(
                item.get("outcome") == "transport_error"
                for item in condition_attempts
            ),
            "device_ids": ";".join(entry.get("device_ids", [])),
            "mixed_device": entry.get("mixed_device", False),
            "failure_fixture_id": entry.get("failure_fixture_id", ""),
        }
        if row["representative_metrics"]:
            row.update(_stats(accepted_by_condition[condition_id]))
        else:
            row.update({field: "" for field in metrics})
        condition_rows.append(row)
    condition_fields = [
        "condition_id",
        "mission",
        "algorithm",
        "top_k_level",
        "top_k_rate",
        "top_k_cells",
        "trial_start_index",
        "trial_count",
        "status",
        "classification",
        "representative_metrics",
        "fixture_total",
        "fixtures_completed",
        "attempt_count",
        "timing_attempt_count",
        "transport_error_count",
        "device_ids",
        "mixed_device",
        "failure_fixture_id",
        *metrics,
    ]
    _atomic_csv(
        output_root / "condition_metrics.csv",
        condition_fields,
        condition_rows,
    )
    manifest = {
        "schema": 1,
        "campaign_id": schedule["campaign_id"],
        "raw_attempts": len(attempts),
        "accepted_calls": len(accepted),
        "feasible_conditions": len(feasible_ids),
        "files": {
            path.name: path.stat().st_size
            for path in sorted(output_root.glob("*.csv"))
        },
    }
    (output_root / "manifest.json").write_text(
        json.dumps(manifest, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    return manifest
