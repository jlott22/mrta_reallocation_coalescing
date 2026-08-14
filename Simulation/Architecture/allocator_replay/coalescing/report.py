"""Tidy call/trial/condition outputs for HIL replay."""

from __future__ import annotations

import csv
import json
import statistics
from collections import defaultdict
from pathlib import Path
from typing import Any, Iterable

from allocator_replay.causal.session import DEVICE_ALLOCATOR_TIMER_SCOPE

from .io import atomic_json, load_json
from .manifests import canonical_sha256
from .schedule import verify_campaign


def _rows(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line]


def _percentile(values: list[float], fraction: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    index = max(0, min(len(ordered) - 1, int((len(ordered) - 1) * fraction + 0.999999)))
    return float(ordered[index])


def _write_csv(path: Path, rows: Iterable[dict[str, Any]]) -> None:
    values = list(rows)
    path.parent.mkdir(parents=True, exist_ok=True)
    fields = sorted({key for row in values for key in row})
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(values)


def rebuild_report(root: Path) -> dict[str, Any]:
    schedule = verify_campaign(Path(root))
    state = load_json(Path(root) / "state.json")
    journal = _rows(Path(root) / "journal" / "attempts.jsonl")
    binding = schedule["device_binding"]
    devices = {str(item["device_id"]): item for item in binding["devices"]}
    for row in journal:
        claimed = str(row.get("record_sha256", ""))
        unsigned = dict(row)
        unsigned.pop("record_sha256", None)
        if not claimed or canonical_sha256(unsigned) != claimed:
            raise ValueError("HIL journal record hash mismatch")
        if row.get("schedule_sha256") != schedule["schedule_sha256"]:
            raise ValueError("HIL journal row belongs to a different schedule")
        if row.get("device_binding_sha256") != binding["device_binding_sha256"]:
            raise ValueError("HIL journal row belongs to a different device binding")
        expected = devices.get(str(row.get("device_id", "")))
        if expected is None:
            raise ValueError("HIL journal row names an unbound device")
        exact_fields = {
            "device_build_id": "build_id",
            "device_module_set_sha256": "module_set_sha256",
            "device_source_bundle_sha256": "source_bundle_sha256",
            "device_firmware_sha256": "firmware_sha256",
            "device_implementation": "implementation",
            "device_frequency_hz": "frequency_hz",
        }
        for journal_name, binding_name in exact_fields.items():
            if row.get(journal_name) != expected.get(binding_name):
                raise ValueError(
                    f"HIL journal {journal_name} differs from device binding"
                )
    accepted_generation: dict[tuple[str, str], int] = {}
    for condition_id, condition_state in state["conditions"].items():
        for pair_id, trial_state in condition_state["trials"].items():
            if trial_state["status"] == "completed":
                accepted_generation[(condition_id, pair_id)] = int(trial_state["generation"])
    calls = [
        row
        for row in journal
        if row.get("record_type") == "allocator_call"
        and accepted_generation.get((row["condition_id"], row["paired_manifest_id"]))
        == int(row["run_generation"])
    ]
    completions = [
        row
        for row in journal
        if row.get("record_type") == "trial_completed"
        and accepted_generation.get((row["condition_id"], row["paired_manifest_id"]))
        == int(row["run_generation"])
    ]
    completion_keys = [
        (row["condition_id"], row["paired_manifest_id"], int(row["run_generation"]))
        for row in completions
    ]
    expected_completion_keys = [
        (condition_id, pair_id, generation)
        for (condition_id, pair_id), generation in accepted_generation.items()
    ]
    if sorted(completion_keys) != sorted(expected_completion_keys):
        raise ValueError("completed trial state lacks one exact sealed journal completion")
    for row in calls:
        if row.get("device_allocator_timer_scope") != (
            DEVICE_ALLOCATOR_TIMER_SCOPE
        ):
            raise ValueError(
                "HIL allocator call has a noncanonical timer scope"
            )
        row["accepted_for_analysis"] = True
    trial_groups: dict[tuple[str, str], list[dict[str, Any]]] = defaultdict(list)
    for row in calls:
        trial_groups[(row["condition_id"], row["paired_manifest_id"])].append(row)
    trial_rows: list[dict[str, Any]] = []
    for (condition_id, pair_id), group in sorted(trial_groups.items()):
        values = [float(item["device_allocator_time_us"]) for item in group]
        first = group[0]
        trial_rows.append(
            {
                "condition_id": condition_id,
                "paired_manifest_id": pair_id,
                "allocator": first["allocator"],
                "arrival_load": first["arrival_load"],
                "policy": first["policy"],
                "policy_id": first["policy_id"],
                "batch_size": first["batch_size"],
                "max_wait_s": first.get("max_wait_s"),
                "candidate_mode": "unrestricted",
                "device_id": first["device_id"],
                "device_binding_sha256": first["device_binding_sha256"],
                "device_build_id": first["device_build_id"],
                "device_module_set_sha256": first["device_module_set_sha256"],
                "device_firmware_sha256": first["device_firmware_sha256"],
                "allocator_call_count": len(group),
                "allocation_epoch_count": len({item["epoch_index"] for item in group}),
                "device_allocator_time_us_total": int(sum(values)),
                "device_allocator_time_us_mean": statistics.fmean(values),
                "device_allocator_time_us_median": statistics.median(values),
                "device_allocator_time_us_p95": _percentile(values, 0.95),
                "device_allocator_time_us_max": max(values),
                "host_nonallocator_overhead_us_total": sum(
                    int(item["host_nonallocator_overhead_us"]) for item in group
                ),
                "candidate_restriction_violations": sum(
                    int(item["candidate_count_before"] != item["candidate_count_after"])
                    for item in group
                ),
                "resident_registry_mismatches": sum(
                    int(
                        item.get("resident_active_task_count")
                        != item.get("host_active_task_count")
                    )
                    for item in group
                ),
            }
        )
    condition_groups: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in trial_rows:
        condition_groups[row["condition_id"]].append(row)
    condition_rows: list[dict[str, Any]] = []
    for condition_id, group in sorted(condition_groups.items()):
        totals = [float(item["device_allocator_time_us_total"]) for item in group]
        first = group[0]
        condition_rows.append(
            {
                "condition_id": condition_id,
                "allocator": first["allocator"],
                "arrival_load": first["arrival_load"],
                "policy_id": first["policy_id"],
                "batch_size": first["batch_size"],
                "max_wait_s": first.get("max_wait_s"),
                "completed_trace_count": len(group),
                "device_allocator_time_us_trial_mean": statistics.fmean(totals),
                "device_allocator_time_us_trial_median": statistics.median(totals),
                "allocator_calls_total": sum(item["allocator_call_count"] for item in group),
                "allocation_epochs_total": sum(item["allocation_epoch_count"] for item in group),
                "candidate_restriction_violations": sum(
                    item["candidate_restriction_violations"] for item in group
                ),
                "resident_registry_mismatches": sum(
                    item["resident_registry_mismatches"] for item in group
                ),
            }
        )
    reports = Path(root) / "reports"
    _write_csv(reports / "allocator_calls.csv", calls)
    _write_csv(reports / "trial_metrics.csv", trial_rows)
    _write_csv(reports / "condition_metrics.csv", condition_rows)
    summary = {
        "schema_version": 2,
        "campaign_id": schedule["campaign_id"],
        "campaign_status": state["status"],
        "accepted_allocator_calls": len(calls),
        "completed_trials": len(trial_rows),
        "reported_conditions": len(condition_rows),
        "device_binding_sha256": binding["device_binding_sha256"],
        "build_id": binding["build_id"],
        "module_set_sha256": binding["module_set_sha256"],
        "firmware_sha256_values": sorted(
            {str(item["firmware_sha256"]) for item in binding["devices"]}
        ),
        "hardware_validated": any(row.get("execution_mode") == "serial_hardware" for row in calls),
        "timing_definition": {
            "device_allocator_time_us": DEVICE_ALLOCATOR_TIMER_SCOPE,
            "host_nonallocator_overhead_us": "setup, USB transport, and output outside device timer",
        },
    }
    atomic_json(reports / "summary.json", summary)
    return summary


def campaign_status(root: Path) -> dict[str, Any]:
    verify_campaign(Path(root))
    state = load_json(Path(root) / "state.json")
    counts: dict[str, int] = defaultdict(int)
    for condition in state["conditions"].values():
        for trial in condition["trials"].values():
            counts[trial["status"]] += 1
    return {"campaign_id": state["campaign_id"], "status": state["status"], "trials": dict(counts)}
