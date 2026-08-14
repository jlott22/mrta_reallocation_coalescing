#!/usr/bin/env python3
"""Build the read-only Paper-V2 latency and mechanism data bundle.

This program reads the immutable August 2026 MRTA campaign artifacts and the
compact publication/aug14_final_v1 export.  It never imports or runs the
simulator.  It normalizes the AGX/zero-compute ``allocation_epochs.csv`` and
RP2040 ``reallocation_events.csv`` layouts, reconstructs task/epoch/call
mechanism fields, audits the old publication values, and writes a new bundle.

The trace is the environmental replication unit.  This exporter creates only
descriptive task/call/epoch values and exact trace-matched deltas; it does not
perform inferential task-level analysis.
"""

from __future__ import annotations

import argparse
import csv
import datetime as dt
import gzip
import hashlib
import io
import itertools
import json
import math
import os
import shutil
import statistics
import subprocess
import sys
import zipfile
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable, Iterator, Mapping, MutableMapping, Sequence


ALGORITHMS = ("CBAA", "ACBBA", "PI", "HIPC")
LOADS = ("low", "medium", "high")
POLICIES = ("eager_b1", "count_b2", "count_b4", "count_b8", "bounded_b4_w5")
ARRIVAL_RATES = {"low": 0.075, "medium": 0.3, "high": 1.2}
CALL_CLASSES = (
    "full_allocation_solve",
    "partial_bundle_refill",
    "candidate_filter_only",
    "cached_or_maintenance",
    "other",
)
EPOCH_REASONS = (
    "initial_allocation",
    "task_arrival_eager",
    "batch_threshold",
    "age_timeout",
    "final_release_flush",
    "task_completion",
    "invalid_goal",
    "robot_idle",
    "other",
)
ARRIVAL_REASONS = {
    "task_arrival_eager",
    "batch_threshold",
    "age_timeout",
    "final_release_flush",
}
MANDATORY_REASONS = {"task_completion", "invalid_goal", "robot_idle"}
TOLERANCE_S = 1e-9
SPLIT_BYTES = 100_000_000

SOURCE_SPECS = {
    "agx_primary_traces_0_24": ("agx_deadline_primary_1396_v1", "causal", "agx_host_proxy", "completed"),
    "hardware_core": ("agx_deadline_hardware_core_96_v1", "causal", "rp2040_hardware", "causal/completed"),
    "hardware_bounded": ("agx_deadline_hardware_bounded_8_v1", "causal", "rp2040_hardware", "causal/completed"),
    "agx_causal_traces_25_49": ("agx_n50_extension_causal_1500_v1", "causal", "agx_host_proxy", "completed"),
    "zero_compute_traces_0_7": ("agx_deadline_zero_compute_480_scheduler_v2", "zero_compute", "zero_compute", "completed"),
    "zero_compute_traces_8_24": ("agx_deadline_zero_compute_traces_8_24_v2", "zero_compute", "zero_compute", "completed"),
    "zero_compute_traces_25_49": ("agx_n50_extension_zero_compute_1500_v1", "zero_compute", "zero_compute", "completed"),
    "arrival_trace_0": ("agx_deadline_arrival_verify_24_v1", "arrival_verification", "agx_host_proxy", "completed"),
    "arrival_traces_1_2": ("agx_deadline_arrival_verify_traces_1_2_v2", "arrival_verification", "agx_host_proxy", "completed"),
    "timeout_trace_0": ("agx_deadline_timeout_verify_24_v1", "timeout_verification", "agx_host_proxy", "completed"),
    "timeout_traces_1_2": ("agx_deadline_timeout_verify_traces_1_2_v2", "timeout_verification", "agx_host_proxy", "completed"),
}

IDENTIFIER_FIELDS = [
    "dataset", "provider", "source_campaign", "job_id", "condition_id",
    "algorithm", "arrival_load", "policy_id", "trace_id", "trace_number",
    "runtime_seed", "board_id", "worker_id", "scenario_sha256", "release_sha256",
]


def _sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _read_bytes(path: Path) -> tuple[bytes, dict[str, Any]]:
    data = path.read_bytes()
    return data, {"path": str(path.resolve()), "size": len(data), "sha256": _sha256_bytes(data)}


def _read_json_hashed(path: Path) -> tuple[dict[str, Any], dict[str, Any]]:
    data, meta = _read_bytes(path)
    value = json.loads(data.decode("utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"expected JSON object: {path}")
    return value, meta


def _read_csv_hashed(path: Path) -> tuple[list[dict[str, str]], dict[str, Any]]:
    data, meta = _read_bytes(path)
    with io.StringIO(data.decode("utf-8"), newline="") as handle:
        rows = list(csv.DictReader(handle))
    meta["rows"] = len(rows)
    return rows, meta


def _read_csv(path: Path) -> tuple[list[dict[str, str]], list[str]]:
    with path.open(newline="", encoding="utf-8") as handle:
        reader = csv.DictReader(handle)
        return list(reader), list(reader.fieldnames or [])


def _number(value: Any) -> float | None:
    if value is None or value == "":
        return None
    try:
        result = float(value)
    except (TypeError, ValueError):
        return None
    return result if math.isfinite(result) else None


def _integer(value: Any) -> int | None:
    number = _number(value)
    return None if number is None else int(number)


def _truth(value: Any) -> bool | None:
    if isinstance(value, bool):
        return value
    if value is None or value == "":
        return None
    text = str(value).strip().lower()
    if text in {"true", "1", "yes"}:
        return True
    if text in {"false", "0", "no"}:
        return False
    return None


def _fmt(value: Any) -> Any:
    if value is None:
        return ""
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (dict, list, tuple, set)):
        return json.dumps(value, sort_keys=True, separators=(",", ":"))
    if isinstance(value, float):
        if not math.isfinite(value):
            return ""
        return repr(value)
    return value


def _slug(value: Any) -> str:
    text = str(value or "other").strip().lower()
    cleaned = "".join(ch if ch.isalnum() else "_" for ch in text)
    return "_".join(part for part in cleaned.split("_") if part) or "other"


def _parse_list(value: Any) -> list[Any]:
    if value is None or value == "":
        return []
    if isinstance(value, list):
        return value
    text = str(value).strip()
    if not text:
        return []
    if ";" in text and not text.startswith("["):
        return [item for item in text.split(";") if item]
    try:
        parsed = json.loads(text)
    except json.JSONDecodeError:
        try:
            import ast
            parsed = ast.literal_eval(text)
        except (ValueError, SyntaxError):
            return [text]
    return list(parsed) if isinstance(parsed, (list, tuple)) else [parsed]


def _parse_cell(value: Any, x: Any = None, y: Any = None) -> tuple[int, int] | None:
    if x not in {None, ""} and y not in {None, ""}:
        try:
            return int(x), int(y)
        except (TypeError, ValueError):
            pass
    values = _parse_list(value)
    if len(values) == 2:
        try:
            return int(values[0]), int(values[1])
        except (TypeError, ValueError):
            return None
    return None


def _trace_number(value: str) -> int:
    return int(str(value).split("_")[-1])


def _trial_key(row: Mapping[str, Any]) -> tuple[str, str, str, str, str]:
    return (
        str(row["dataset"]), str(row["algorithm"]), str(row["arrival_load"]),
        str(row["policy_id"]), str(row["trace_id"]),
    )


def _scientific_key(row: Mapping[str, Any]) -> tuple[str, str, str, str]:
    return (
        str(row["algorithm"]), str(row["arrival_load"]),
        str(row["policy_id"]), str(row["trace_id"]),
    )


def _sort_key(row: Mapping[str, Any]) -> tuple[Any, ...]:
    return (
        ("causal", "zero_compute", "arrival_verification", "timeout_verification").index(str(row.get("dataset")))
        if str(row.get("dataset")) in {"causal", "zero_compute", "arrival_verification", "timeout_verification"} else 99,
        str(row.get("provider", "")),
        ALGORITHMS.index(str(row.get("algorithm"))) if str(row.get("algorithm")) in ALGORITHMS else 99,
        LOADS.index(str(row.get("arrival_load"))) if str(row.get("arrival_load")) in LOADS else 99,
        POLICIES.index(str(row.get("policy_id"))) if str(row.get("policy_id")) in POLICIES else 99,
        _integer(row.get("trace_number")) if _integer(row.get("trace_number")) is not None else _trace_number(str(row.get("trace_id", "trace_9999"))),
    )


def _quantile(values: Sequence[float], proportion: float) -> float | None:
    clean = sorted(float(value) for value in values if math.isfinite(float(value)))
    if not clean:
        return None
    if len(clean) == 1:
        return clean[0]
    h = (len(clean) - 1) * proportion
    lo = math.floor(h)
    hi = math.ceil(h)
    if lo == hi:
        return clean[lo]
    return clean[lo] + (h - lo) * (clean[hi] - clean[lo])


def _stats(values: Iterable[Any]) -> dict[str, Any]:
    clean = [number for value in values if (number := _number(value)) is not None]
    return {
        "count": len(clean),
        "mean": statistics.fmean(clean) if clean else None,
        "median": _quantile(clean, 0.5),
        "q1": _quantile(clean, 0.25),
        "q3": _quantile(clean, 0.75),
        "q90": _quantile(clean, 0.90),
        "q95": _quantile(clean, 0.95),
        "max": max(clean) if clean else None,
    }


def _add_stats(target: MutableMapping[str, Any], prefix: str, values: Iterable[Any], *, full: bool = True) -> None:
    summary = _stats(values)
    names = ("count", "mean", "median", "q1", "q3", "q90", "q95", "max") if full else ("mean", "median", "q90", "max")
    for name in names:
        suffix = "" if name == "count" else "_s"
        target[f"{prefix}_{name}{suffix}"] = summary[name]


def _counter_json(counter: Mapping[str, int]) -> str:
    return json.dumps(dict(sorted(counter.items())), separators=(",", ":"))


def _dominant(counter: Mapping[str, int]) -> str | None:
    if not counter:
        return None
    return sorted(counter, key=lambda key: (-counter[key], key))[0]


def _hash_sequence(values: Iterable[Any]) -> str:
    data = json.dumps(list(values), sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(data).hexdigest()


def _interval_union(intervals: Iterable[tuple[float, float]]) -> tuple[float, list[tuple[float, float]]]:
    ordered = sorted((float(start), float(end)) for start, end in intervals if end >= start)
    merged: list[list[float]] = []
    for start, end in ordered:
        if not merged or start > merged[-1][1] + TOLERANCE_S:
            merged.append([start, end])
        else:
            merged[-1][1] = max(merged[-1][1], end)
    pairs = [(start, end) for start, end in merged]
    return sum(end - start for start, end in pairs), pairs


def _interval_intersection_s(intervals: Iterable[tuple[float, float]], start: float | None, end: float | None) -> float | None:
    if start is None or end is None:
        return None
    _, merged = _interval_union(intervals)
    return sum(max(0.0, min(right, end) - max(left, start)) for left, right in merged)


def _contains_time(intervals: Sequence[tuple[float, float]], time_s: float) -> bool:
    return any(start - TOLERANCE_S <= time_s <= end + TOLERANCE_S for start, end in intervals)


class CsvRegistry:
    def __init__(self, root: Path) -> None:
        self.root = root
        self.records: dict[str, dict[str, Any]] = {}

    def record(self, path: Path, row_count: int, fields: Sequence[str]) -> None:
        relative = str(path.relative_to(self.root))
        self.records[relative] = {"row_count": int(row_count), "column_count": len(fields), "columns": list(fields)}

    def write(self, relative: str, rows: Iterable[Mapping[str, Any]], fields: Sequence[str]) -> Path:
        path = self.root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        count = 0
        with path.open("w", newline="", encoding="utf-8") as handle:
            writer = csv.DictWriter(handle, fieldnames=list(fields), extrasaction="ignore", lineterminator="\n")
            writer.writeheader()
            for row in rows:
                writer.writerow({key: _fmt(row.get(key)) for key in fields})
                count += 1
        self.record(path, count, fields)
        return path


class GzipCsvWriter:
    """Deterministic gzip CSV writer (mtime=0)."""

    def __init__(self, registry: CsvRegistry, relative: str, fields: Sequence[str]) -> None:
        self.registry = registry
        self.path = registry.root / relative
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.fields = list(fields)
        self.count = 0
        self._raw = self.path.open("wb")
        self._gzip = gzip.GzipFile(filename="", mode="wb", fileobj=self._raw, mtime=0, compresslevel=6)
        self._text = io.TextIOWrapper(self._gzip, encoding="utf-8", newline="")
        self._writer = csv.DictWriter(self._text, fieldnames=self.fields, extrasaction="ignore", lineterminator="\n")
        self._writer.writeheader()

    def writerow(self, row: Mapping[str, Any]) -> None:
        self._writer.writerow({key: _fmt(row.get(key)) for key in self.fields})
        self.count += 1

    def close(self) -> None:
        if self._text.closed:
            return
        self._text.flush()
        self._text.close()
        # GzipFile deliberately does not close a caller-provided fileobj.
        # Close the raw BufferedWriter before hashing/packaging so its final
        # bytes cannot be flushed after export_manifest.json is generated.
        if not self._raw.closed:
            self._raw.flush()
            self._raw.close()
        self.registry.record(self.path, self.count, self.fields)

    def __enter__(self) -> "GzipCsvWriter":
        return self

    def __exit__(self, *_: Any) -> None:
        self.close()


@dataclass
class RawTrial:
    publication: dict[str, str]
    source_label: str
    campaign_root: Path
    artifact_dir: Path
    summary: dict[str, Any]
    summary_meta: dict[str, Any]
    retained_attempt_id: str
    historical_failed_attempts: int
    retained_audited_outcome: bool
    raw_job_dir_name: str

    @property
    def dataset(self) -> str:
        return self.publication["dataset"]

    @property
    def provider(self) -> str:
        return self.publication["provider"]

    @property
    def key(self) -> tuple[str, str, str, str, str]:
        return _trial_key(self.publication)


@dataclass
class TrialResult:
    row: dict[str, Any]
    tasks: list[dict[str, Any]]
    epoch_rows: list[dict[str, Any]]
    calls: list[dict[str, Any]]
    movement_signature_sha256: str
    assignment_signature_sha256: str
    completion_signature_sha256: str
    semantic_events: list[dict[str, Any]] = field(default_factory=list)


LATENCY_SCOPES = ("all", "initial", "online", "online_scheduler_admitted")
LATENCY_FAMILIES = (
    "release_to_admission",
    "admission_to_first_any_assignment",
    "admission_to_first_allocator_assignment",
    "release_to_first_any_assignment",
    "release_to_first_allocator_assignment",
    "first_any_assignment_to_completion",
    "release_to_completion",
)


def _trial_append_fields() -> list[str]:
    fields = [
        "arrival_rate_tasks_per_s", "mean_interarrival_s", "initial_task_count",
        "online_task_count", "first_online_release_time_s", "last_release_time_s",
        "released_task_count_at_end", "admitted_task_count_at_end",
        "assigned_task_count_at_end", "allocator_assigned_task_count_at_end",
        "service_fallback_assigned_task_count", "completed_task_count_at_end",
        "next_release_after_horizon_s", "mission_elapsed_minus_last_release_s",
    ]
    for scope in LATENCY_SCOPES:
        for family in LATENCY_FAMILIES:
            for stat in ("count", "mean", "median", "q1", "q3", "q90", "q95", "max"):
                fields.append(f"{scope}_{family}_{stat}{'' if stat == 'count' else '_s'}")
    fields.extend([
        "online_admission_to_assignment_mean_s", "online_assignment_to_completion_mean_s",
        "online_release_to_first_assignment_mean_s",
        "online_service_latency_release_to_admission_fraction",
        "online_service_latency_admission_to_assignment_fraction",
        "online_service_latency_assignment_to_completion_fraction",
        "online_service_latency_fraction_method",
        "admission_epoch_count", "initial_allocation_admitted_task_count",
        "eager_arrival_admitted_task_count", "batch_threshold_admitted_task_count",
        "age_timeout_admitted_task_count", "final_release_flush_admitted_task_count",
        "mandatory_piggyback_admitted_task_count",
        "admitted_batch_size_mean", "admitted_batch_size_median", "admitted_batch_size_q90", "admitted_batch_size_max",
        "event_sampled_pending_age_at_admission_mean_s", "event_sampled_pending_age_at_admission_median_s",
        "event_sampled_pending_age_at_admission_q90_s", "event_sampled_pending_age_at_admission_max_s",
        "online_release_to_admission_task_weighted_mean_s", "online_release_to_admission_task_weighted_median_s",
        "online_release_to_admission_task_weighted_q90_s", "online_release_to_admission_task_weighted_max_s",
        "bounded_timeout_admission_count", "bounded_tasks_admitted_at_approximately_w5_count",
        "bounded_pending_age_violation_count", "final_partial_batch_flush_count",
        "mandatory_piggyback_admission_count", "task_weighted_vs_event_sampled_queue_metrics_distinct",
        "total_epoch_count", "initial_epoch_count", "mandatory_epoch_count",
        "mandatory_task_completion_epoch_count", "mandatory_invalid_goal_epoch_count", "mandatory_robot_idle_epoch_count",
        "local_epoch_count", "global_epoch_count", "total_valid_allocator_call_count",
        "invalid_or_parity_rejected_call_count", "calls_per_epoch", "calls_per_admission_epoch",
        "calls_per_mandatory_epoch", "calls_per_admitted_online_task", "expected_first_round_call_count",
        "observed_first_round_call_count", "post_first_round_call_count", "post_closure_call_count",
        "calls_associated_with_admission_epochs", "calls_associated_with_mandatory_epochs",
        "calls_with_current_goal_count", "calls_with_no_current_goal_count",
        "calls_that_changed_goal_count", "calls_that_retained_goal_count",
        "unique_robots_invoked_per_epoch_mean", "unique_robots_invoked_per_epoch_max",
    ])
    for value in CALL_CLASSES:
        fields.extend([f"call_class_{value}_count", f"call_class_{value}_work_s"])
    for value in EPOCH_REASONS:
        fields.extend([f"parent_epoch_reason_{value}_call_count", f"parent_epoch_reason_{value}_work_s"])
    fields.extend([
        "agx_choose_goal_work_s", "agx_epoch_reset_work_s", "total_agx_allocator_work_s",
        "rp2040_choose_goal_work_s", "rp2040_epoch_reset_work_s", "total_rp2040_allocator_work_s",
        "injected_provider_work_s", "zero_compute_injected_work_s", "provider_allocator_processor_work_s",
        "provider_allocator_processor_work_definition", "per_call_duration_median_s", "per_call_duration_q90_s",
        "per_call_duration_q95_s", "per_call_duration_max_s", "median_per_call_rp2040_to_agx_ratio",
        "summed_valid_call_duration_s", "virtual_compute_union_diagnostic_s",
        "summed_per_robot_compute_union_diagnostic_s", "mean_per_robot_compute_busy_diagnostic_s",
        "max_per_robot_compute_busy_diagnostic_s", "mean_per_robot_compute_busy_fraction_diagnostic",
        "max_per_robot_compute_busy_fraction_diagnostic", "max_simultaneous_logical_compute_count_diagnostic",
        "mean_logical_compute_concurrency_while_active_diagnostic",
        "release_events_during_virtual_compute_count", "admission_epochs_during_virtual_compute_count",
        "buffered_message_count", "maximum_compute_buffering_delay_s",
        "total_assignment_event_count", "first_assignment_count", "repeated_same_owner_assignment_event_count",
        "distinct_owner_change_count", "total_task_reassignment_count", "mean_unique_owners_per_task",
        "max_unique_owners_per_task", "task_goal_change_count", "robot_goal_change_count",
        "bundle_reset_count", "assignment_churn_per_completed_task", "reconstructed_max_robot_steps",
        "reconstructed_total_team_steps", "turn_count", "replan_count", "blocked_intent_collision_count",
        "robot_idle_event_count", "invalid_goal_event_count", "quarantine_count",
        "final_completed_task_id", "final_completed_task_cell", "final_completion_robot",
        "final_task_release_time_s", "final_task_admission_time_s", "final_task_first_assignment_time_s",
        "final_task_first_assignment_source", "final_task_completion_time_s", "final_task_was_last_released",
        "final_task_release_to_admission_s", "final_task_admission_to_assignment_s",
        "final_task_release_to_assignment_s", "final_task_assignment_to_completion_s",
        "final_task_completing_robot_compute_busy_release_to_admission_s",
        "final_task_completing_robot_compute_busy_admission_to_assignment_s",
        "final_task_completing_robot_compute_busy_assignment_to_completion_s",
        "final_task_global_compute_union_release_to_admission_s",
        "final_task_global_compute_union_admission_to_assignment_s",
        "final_task_global_compute_union_assignment_to_completion_s",
        "final_task_timing_is_formal_critical_path", "algorithmic_horizon_type", "horizon_time_s",
        "total_processed_event_count", "stagnation_event_count", "last_progress_timestamp_s",
        "elapsed_since_last_progress_s", "released_count_at_stop", "admitted_count_at_stop",
        "assigned_count_at_stop", "completed_count_at_stop", "next_scheduled_release_after_stop_s",
        "calls_since_last_progress", "epochs_since_last_progress", "dominant_final_window_call_class",
        "dominant_final_window_epoch_reason", "repeated_final_state_signature_count",
        "final_robot_positions", "final_active_task_count", "final_pending_task_count",
        "final_500_semantic_event_call_classes", "final_1000_semantic_event_call_classes",
        "final_500_semantic_event_epoch_reasons", "final_1000_semantic_event_epoch_reasons",
    ])
    return fields


TRIAL_APPEND_FIELDS = _trial_append_fields()


TASK_FIELDS = IDENTIFIER_FIELDS + [
    "arrival_rate_tasks_per_s", "batch_size", "maximum_wait_s", "trace_id_number",
    "task_id", "task_x", "task_y", "release_order", "is_initial_task", "is_online_task",
    "release_time_s", "was_released_by_trial_end", "previous_interarrival_s", "next_interarrival_s",
    "simultaneous_release_group_size", "task_state_at_trial_end", "active_task_count_at_release",
    "pending_depth_immediately_after_release", "minimum_robot_distance_at_release",
    "admission_time_s", "admission_epoch_id", "raw_admission_reason", "normalized_admission_reason",
    "admitted_by_mandatory_piggyback", "queue_depth_immediately_before_admission", "admitted_batch_size",
    "pending_age_at_admission_s", "release_to_admission_latency_s", "bounded_wait_deadline_s",
    "bounded_wait_compliance", "first_any_assignment_time_s", "first_allocator_assignment_time_s",
    "first_assignment_source", "first_assigned_robot", "first_assignment_call_id", "first_assignment_epoch_id",
    "assignment_event_count", "repeated_same_owner_assignment_count", "distinct_owner_change_count",
    "unique_owner_count", "final_assigned_owner", "release_to_first_any_assignment_latency_s",
    "release_to_first_allocator_assignment_latency_s", "admission_to_first_any_assignment_latency_s",
    "admission_to_first_allocator_assignment_latency_s", "completion_time_s", "completion_robot",
    "first_any_assignment_to_completion_latency_s", "release_to_completion_latency_s",
    "mission_ending_task", "last_released_task", "allocator_only_decomposition_defined",
]


EPOCH_FIELDS = IDENTIFIER_FIELDS + [
    "epoch_id", "virtual_epoch_start_time_s", "epoch_close_time_s", "last_associated_call_completion_time_s",
    "raw_epoch_reason", "normalized_epoch_reason", "mandatory", "initial", "global_or_local",
    "initiating_robot", "admission", "admitted_task_count", "admitted_task_ids_sha256",
    "pending_depth_before_admission", "pending_depth_after_admission", "oldest_pending_age_before_admission_s",
    "maximum_admitted_task_pending_age_s", "batch_threshold", "timeout_deadline_s",
    "timeout_lateness_s", "piggyback", "expected_first_round_robot_count",
    "observed_first_round_robot_count", "total_associated_calls", "post_first_round_calls",
    "post_closure_calls", "unique_robots_called", "calls_by_normalized_class",
    "agx_call_work_sum_s", "rp2040_call_work_sum_s", "injected_call_work_sum_s",
    "first_round_compute_makespan_s", "total_associated_call_span_s", "per_call_duration_mean_s",
    "per_call_duration_median_s", "per_call_duration_max_s", "active_task_count_at_epoch_start",
    "candidate_count_mean", "candidate_count_max", "inbound_message_count", "outbound_message_count",
    "goal_change_count", "assignment_event_count", "owner_change_count", "completed_tasks_since_previous_epoch",
    "robot_steps_since_previous_epoch", "time_since_previous_epoch_s", "time_to_next_epoch_s",
    "epoch_close_semantics", "expected_robot_set_derivation",
]


CALL_SUMMARY_FIELDS = IDENTIFIER_FIELDS + [
    "robot_id", "normalized_epoch_reason", "normalized_call_class", "first_round_status",
    "post_closure_status", "call_count", "agx_work_sum_s", "rp2040_work_sum_s",
    "injected_work_sum_s", "call_duration_mean_s", "call_duration_median_s",
    "call_duration_q90_s", "call_duration_q95_s", "call_duration_max_s",
    "candidate_count_mean", "candidate_count_median", "candidate_count_q90", "candidate_count_max",
    "active_task_count_mean", "active_task_count_median", "active_task_count_max",
    "inbound_message_count", "outbound_message_count", "goal_change_count", "goal_change_rate",
    "assignment_event_count", "parity_failure_count", "buffered_event_count",
]


HARDWARE_CALL_FIELDS = IDENTIFIER_FIELDS + [
    "board_uid", "robot_context_id", "call_id", "parent_epoch_id", "epoch_reason", "call_class",
    "first_round", "post_closure", "physical_measurement_sequence_number", "virtual_request_time_s",
    "virtual_start_time_s", "virtual_completion_time_s", "virtual_queue_wait_s", "injected_duration_s",
    "agx_authoritative_choose_goal_duration_s", "agx_epoch_reset_duration_s", "rp2040_choose_goal_duration_s",
    "rp2040_epoch_reset_duration_s", "total_rp2040_device_work_s", "rp2040_to_agx_ratio",
    "physical_host_wall_measurement_start_s", "physical_host_wall_measurement_end_s", "psetup_duration_s",
    "ptime_duration_s", "result_serialization_duration_s", "total_serial_round_trip_s", "protocol_retry_count",
    "device_timeout", "active_task_count", "candidate_count", "admitted_batch_size", "goal_before",
    "goal_after", "goal_change", "inbound_message_count", "outbound_message_count", "pre_state_hash",
    "post_state_hash", "output_parity_result", "parity_path_or_comparison_mode", "validity_for_mission",
    "simultaneous_hardware_measurement_count", "overlapping_agx_authoritative_measurement_count",
    "worker_concurrency_at_measurement_start", "physical_concurrency_evidence_available",
]


REP_EVENT_FIELDS = [
    "case_id", "case_label", "comparison_role", "record_type", *IDENTIFIER_FIELDS,
    "sequence_index", "event_time_s", "source_stream", "event_kind", "robot_id", "task_id",
    "epoch_id", "call_id", "reason_or_class", "goal", "owner", "state_hash", "details_sha256",
    "released_task_count", "admitted_task_count", "completed_task_count", "progress_signature_sha256",
    "release_time_s", "admission_time_s", "first_assignment_time_s", "completion_time_s",
    "release_to_admission_latency_s", "admission_to_first_assignment_latency_s",
    "first_assignment_to_completion_latency_s", "release_to_completion_latency_s",
]


def _locate_campaign(raw_root: Path, campaign_name: str) -> Path:
    candidates = [
        raw_root / campaign_name,
        raw_root / "study" / "output" / campaign_name,
        raw_root / "study" / "output" / "agx_deadline_aug14_v1" / "fix_worktree" / "study" / "output" / campaign_name,
        raw_root / "agx_deadline_aug14_v1" / "fix_worktree" / "study" / "output" / campaign_name,
    ]
    for candidate in candidates:
        if candidate.is_dir():
            return candidate.resolve()
    matches = [
        path for path in raw_root.glob(f"**/{campaign_name}")
        if path.is_dir() and "paper_v2_data_bundle" not in path.parts
    ]
    if len(matches) == 1:
        return matches[0].resolve()
    if not matches:
        raise FileNotFoundError(f"cannot locate raw campaign {campaign_name} below {raw_root}")
    raise ValueError(f"ambiguous raw campaign {campaign_name}: {matches}")


def _resolve_stale_path(path_text: Any, repository_root: Path, raw_root: Path) -> Path | None:
    if not path_text:
        return None
    path = Path(str(path_text))
    if path.is_file():
        return path.resolve()
    anchors = ("study/generated/", "configs/", "artifacts/")
    normalized = str(path).replace("\\", "/")
    for anchor in anchors:
        if anchor in normalized:
            suffix = normalized.split(anchor, 1)[1]
            for base in (
                repository_root / anchor.rstrip("/"),
                raw_root / anchor.rstrip("/"),
                repository_root / "study/output/agx_deadline_aug14_v1/fix_worktree" / anchor.rstrip("/"),
            ):
                candidate = base / suffix
                if candidate.is_file():
                    return candidate.resolve()
    return None


def _manifest_hash_index(repository_root: Path, raw_root: Path) -> dict[str, Path]:
    roots = {
        repository_root / "study/generated/manifests",
        raw_root / "study/generated/manifests",
        repository_root / "study/output/agx_deadline_aug14_v1/fix_worktree/study/generated/manifests",
        raw_root / "study/output/agx_deadline_aug14_v1/fix_worktree/study/generated/manifests",
    }
    result: dict[str, Path] = {}
    for root in sorted(roots):
        if not root.is_dir():
            continue
        for path in sorted(root.rglob("*.json")):
            digest = _sha256(path)
            prior = result.get(digest)
            if prior is None or len(str(path)) < len(str(prior)):
                result[digest] = path.resolve()
    return result


def _raw_identity(summary: Mapping[str, Any], artifact_dir: Path) -> tuple[str, str, str, str]:
    algorithm = str(summary.get("algorithm") or "")
    load = str(summary.get("arrival_load") or summary.get("load_id") or "")
    policy = str(summary.get("policy_id") or "")
    trace = str(summary.get("trace_id") or summary.get("trial_id") or "")
    if not trace.startswith("trace_"):
        for token in artifact_dir.name.split("__"):
            if token.startswith("trace_"):
                trace = token
                break
    return algorithm, load, policy, trace


def _load_campaign_trials(
    source_label: str,
    spec: tuple[str, str, str, str],
    campaign_root: Path,
) -> dict[tuple[str, str, str, str], tuple[Path, dict[str, Any], dict[str, Any], bool, str]]:
    _, _, _, completed_subdir = spec
    output: dict[tuple[str, str, str, str], tuple[Path, dict[str, Any], dict[str, Any], bool, str]] = {}
    completed = campaign_root / completed_subdir
    if not completed.is_dir():
        raise FileNotFoundError(f"missing completed directory for {source_label}: {completed}")
    for path in sorted(completed.glob("*/trial_summary.json")):
        summary, meta = _read_json_hashed(path)
        key = _raw_identity(summary, path.parent)
        if key in output:
            raise ValueError(f"duplicate completed raw trial in {source_label}: {key}")
        output[key] = (path.parent.resolve(), summary, meta, False, path.parent.name)
    audit_path = campaign_root / "retained_algorithmic_outcomes.json"
    if audit_path.is_file():
        audit = json.loads(audit_path.read_text(encoding="utf-8"))
        for outcome in audit.get("outcomes", []):
            attempt = Path(str(outcome["retained_attempt"]))
            if not attempt.is_dir():
                attempt = campaign_root / "attempts" / str(outcome["job_id"]) / attempt.name
            path = attempt / "trial_summary.json"
            summary, meta = _read_json_hashed(path)
            key = _raw_identity(summary, attempt)
            if key in output:
                raise ValueError(f"retained audit duplicates a promoted raw trial in {source_label}: {key}")
            output[key] = (attempt.resolve(), summary, meta, True, attempt.name)
    return output


def _historical_failure_count(campaign_root: Path, raw_job_dir_name: str, retained: Path) -> int:
    attempts_candidates = [
        campaign_root / "attempts" / raw_job_dir_name,
        campaign_root / "causal" / "attempts" / raw_job_dir_name,
    ]
    failures: set[Path] = set()
    for root in attempts_candidates:
        if root.is_dir():
            failures.update(path.resolve() for path in root.glob("attempt_*/failure.json"))
    # A retained algorithmic outcome has a historical failure marker by
    # construction, but it is a scientific outcome rather than an unresolved
    # technical attempt.  Count other failed attempts as historical technical
    # attempts; retain the outcome marker separately in the artifact index.
    retained_failure = (retained / "failure.json").resolve()
    return len([path for path in failures if path != retained_failure])


def _discover_trials(
    publication_rows: Sequence[dict[str, str]],
    repository_root: Path,
    raw_root: Path,
) -> tuple[dict[tuple[str, str, str, str, str], RawTrial], dict[str, Path]]:
    campaigns: dict[str, Path] = {}
    loaded: dict[str, dict[tuple[str, str, str, str], tuple[Path, dict[str, Any], dict[str, Any], bool, str]]] = {}
    for label in sorted({row["source_campaign"] for row in publication_rows}):
        if label not in SOURCE_SPECS:
            raise ValueError(f"publication references an unknown source campaign: {label}")
        spec = SOURCE_SPECS[label]
        campaign = _locate_campaign(raw_root, spec[0])
        campaigns[label] = campaign
        loaded[label] = _load_campaign_trials(label, spec, campaign)
    output: dict[tuple[str, str, str, str, str], RawTrial] = {}
    for publication in publication_rows:
        label = publication["source_campaign"]
        lookup = (
            publication["algorithm"], publication["arrival_load"],
            publication["policy_id"], publication["trace_id"],
        )
        match = loaded[label].get(lookup)
        if match is None:
            raise KeyError(f"publication trial has no retained raw artifact: {label} {lookup}")
        artifact, summary, meta, audited, attempt_id = match
        campaign = campaigns[label]
        raw_job_name = artifact.parent.name if audited else artifact.name
        failures = _historical_failure_count(campaign, raw_job_name, artifact)
        trial = RawTrial(
            publication=dict(publication), source_label=label, campaign_root=campaign,
            artifact_dir=artifact, summary=summary, summary_meta=meta,
            retained_attempt_id=attempt_id, historical_failed_attempts=failures,
            retained_audited_outcome=audited, raw_job_dir_name=raw_job_name,
        )
        if trial.key in output:
            raise ValueError(f"duplicate publication trial key: {trial.key}")
        output[trial.key] = trial
    return output, campaigns


def _base_identifiers(trial: RawTrial) -> dict[str, Any]:
    row = trial.publication
    worker = trial.summary.get("worker_index")
    board = row.get("board_id") or trial.summary.get("board_id")
    return {
        "dataset": row["dataset"], "provider": row["provider"],
        "source_campaign": row["source_campaign"], "job_id": row["job_id"],
        "condition_id": row["condition_id"], "algorithm": row["algorithm"],
        "arrival_load": row["arrival_load"], "policy_id": row["policy_id"],
        "trace_id": row["trace_id"], "trace_number": _trace_number(row["trace_id"]),
        "runtime_seed": row.get("runtime_seed") or trial.summary.get("runtime_seed"),
        "board_id": board, "worker_id": worker,
        "scenario_sha256": row.get("scenario_sha256") or trial.summary.get("scenario_sha256"),
        "release_sha256": row.get("release_sha256") or trial.summary.get("release_sha256"),
    }


def _normalize_reason(reason: Any) -> str:
    value = str(reason or "other")
    return value if value in EPOCH_REASONS else "other"


def _normalize_class(value: Any) -> str:
    text = str(value or "other")
    return text if text in CALL_CLASSES else "other"


def _epoch_id_from_raw(value: Any) -> str:
    text = str(value or "")
    if not text:
        return ""
    if "/event-" in text:
        return str(int(text.rsplit("-", 1)[-1]))
    try:
        return str(int(float(text)))
    except ValueError:
        return text


def _normalize_epochs(trial: RawTrial, raw: Sequence[Mapping[str, str]]) -> list[dict[str, Any]]:
    hardware = trial.provider == "rp2040_hardware"
    output: list[dict[str, Any]] = []
    for item in raw:
        if hardware:
            epoch_id = _epoch_id_from_raw(item.get("event_id"))
            reason = str(item.get("trigger_reason") or "other")
            admitted_ids = [str(value) for value in _parse_list(item.get("admitted_task_ids"))]
            admitted_count = _integer(item.get("admitted_task_count")) or 0
            called = [str(value) for value in _parse_list(item.get("participating_logical_robots"))]
            associated = [str(value) for value in _parse_list(item.get("associated_call_ids"))]
            mandatory = str(item.get("trigger_class")) == "mandatory"
            initial = reason == "initial_allocation"
            expected_count = 4 if (initial or admitted_count > 0) else 1
            expected = called[:expected_count]
            opened = _number(item.get("event_time_s"))
            closed = _number(item.get("virtual_event_completion_s"))
            pending = _integer(item.get("pending_count")) or 0
            oldest = _number(item.get("oldest_pending_age_s")) or 0.0
            piggyback = _truth(item.get("piggybacked")) is True
            closure = "first_expected_allocator_call_per_robot_not_consensus_convergence"
            expected_derivation = "hardware_export_inference: four for initial/admission epochs; first observed robot for local mandatory epochs"
        else:
            epoch_id = _epoch_id_from_raw(item.get("epoch_id"))
            reason = str(item.get("trigger_reason") or "other")
            admitted_ids = [str(value) for value in _parse_list(item.get("admitted_task_ids"))]
            admitted_count = _integer(item.get("admitted_count")) or len(admitted_ids)
            expected = [str(value) for value in _parse_list(item.get("expected_robot_ids"))]
            called = [str(value) for value in _parse_list(item.get("called_robot_ids"))]
            associated = [str(value) for value in _parse_list(item.get("allocator_call_ids"))]
            mandatory = _truth(item.get("mandatory")) is True
            initial = reason == "initial_allocation"
            expected_count = len(expected)
            opened = _number(item.get("opened_time_s"))
            closed = _number(item.get("closed_time_s"))
            pending = _integer(item.get("pending_depth_before")) or 0
            oldest = _number(item.get("oldest_pending_age_s")) or 0.0
            piggyback = _truth(item.get("piggybacked_pending")) is True
            closure = str(item.get("closure_semantics") or "first_expected_allocator_call_per_robot_not_consensus_convergence")
            expected_derivation = "raw expected_robot_ids"
        output.append({
            "epoch_id": epoch_id, "opened_time_s": opened, "closed_time_s": closed,
            "reason_raw": reason, "reason": _normalize_reason(reason), "mandatory": mandatory,
            "initial": initial, "piggyback": piggyback, "pending_before": pending,
            "oldest_pending_age_s": oldest, "admitted_task_ids": admitted_ids,
            "admitted_count": admitted_count, "expected_robot_ids": expected,
            "expected_count": expected_count, "called_robot_ids_raw": called,
            "associated_call_ids_raw": associated, "closure_semantics": closure,
            "expected_derivation": expected_derivation,
        })
    output.sort(key=lambda row: ((row["opened_time_s"] if row["opened_time_s"] is not None else math.inf), str(row["epoch_id"])))
    return output


def _normalize_calls(trial: RawTrial, raw: Sequence[Mapping[str, str]], epochs: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    hardware = trial.provider == "rp2040_hardware"
    epoch_by_id = {str(epoch["epoch_id"]): epoch for epoch in epochs}
    output: list[dict[str, Any]] = []
    previous_goal_by_robot: dict[str, tuple[int, int] | None] = {}
    for index, item in enumerate(raw, 1):
        if hardware:
            call_id = str(item.get("call_id") or index)
            robot = str(item.get("logical_robot_id") or item.get("context_id") or "")
            epoch_id = _epoch_id_from_raw(item.get("event_id"))
            start = _number(item.get("virtual_compute_start_s"))
            completion = _number(item.get("virtual_compute_completion_s"))
            injected = _number(item.get("rp2040_device_duration_s")) or 0.0
            agx_total = _number(item.get("agx_allocator_duration_s")) or 0.0
            agx_choose = _number(item.get("agx_choose_goal_duration_s")) or agx_total
            agx_reset = _number(item.get("agx_algorithm_epoch_reset_duration_s")) or 0.0
            rp_choose = _number(item.get("rp2040_choose_goal_duration_s")) or 0.0
            rp_reset = _number(item.get("rp2040_algorithm_epoch_reset_duration_s")) or 0.0
            goal_after = _parse_cell(item.get("agx_selected_goal"))
            pre_hash = item.get("pre_state_hash") or ""
            post_hash = item.get("agx_state_hash") or ""
            parity = _truth(item.get("parity_passed"))
            valid = parity is True
            context = item.get("context_id") or robot
            physical_index = _integer(item.get("physical_measurement_index")) or 0
            board_uid = item.get("device_uid") or item.get("expected_device_uid") or ""
            serial = _number(item.get("serial_roundtrip_s"))
            psetup = _number(item.get("psetup_transaction_s"))
            ptime = _number(item.get("ptime_result_transaction_s"))
            parity_mode = item.get("parity_level") or item.get("hardware_validation_mode") or ""
            board_id = item.get("board_id") or trial.publication.get("board_id") or ""
        else:
            call_id = str(item.get("provider_call_id") or item.get("call_id") or index)
            robot = str(item.get("robot_id") or item.get("logical_context_id") or "")
            epoch_id = _epoch_id_from_raw(item.get("epoch_id"))
            start = _number(item.get("compute_start_time_s") or item.get("mission_time_s"))
            completion = _number(item.get("compute_completion_time_s"))
            injected = _number(item.get("device_allocator_duration_s") or item.get("duration_s")) or 0.0
            agx_total = _number(item.get("agx_allocator_duration_s")) or 0.0
            agx_choose = _number(item.get("agx_choose_goal_duration_s")) or 0.0
            agx_reset = _number(item.get("agx_algorithm_epoch_reset_duration_s")) or 0.0
            rp_choose = _number(item.get("device_choose_goal_duration_s")) or 0.0
            rp_reset = _number(item.get("algorithm_epoch_reset_duration_s")) or 0.0
            goal_after = _parse_cell(item.get("authoritative_goal"))
            pre_hash = item.get("pre_state_sha256") or ""
            post_hash = item.get("authoritative_post_state_sha256") or ""
            parity = _truth(item.get("parity_passed"))
            valid = _truth(item.get("valid_for_mission")) is not False and parity is not False
            context = item.get("logical_context_id") or robot
            physical_index = _integer(item.get("physical_measurement_index")) or 0
            board_uid = ""
            serial = _number(item.get("serial_roundtrip_s"))
            psetup = _number(item.get("psetup_transaction_s"))
            ptime = _number(item.get("ptime_result_transaction_s"))
            parity_mode = item.get("timing_source") or ""
            board_id = item.get("board_id") or ""
        epoch = epoch_by_id.get(epoch_id)
        parent_reason = epoch["reason"] if epoch is not None else _normalize_reason(item.get("trigger_reason"))
        raw_reason = str(item.get("trigger_reason") or (epoch["reason_raw"] if epoch else "other"))
        goal_before = previous_goal_by_robot.get(robot)
        goal_changed = goal_before != goal_after
        previous_goal_by_robot[robot] = goal_after
        output.append({
            "call_id": call_id, "robot_id": robot, "context_id": str(context), "epoch_id": epoch_id,
            "raw_reason": raw_reason, "parent_reason": parent_reason,
            "call_class": _normalize_class(item.get("call_class")), "start_s": start,
            "completion_s": completion, "injected_s": injected, "agx_total_s": agx_total,
            "agx_choose_s": agx_choose, "agx_reset_s": agx_reset,
            "rp_choose_s": rp_choose, "rp_reset_s": rp_reset,
            "valid": valid, "parity": parity, "active_count": _integer(item.get("active_task_count")),
            "candidate_count": _integer(item.get("candidate_count")), "goal_before": goal_before,
            "goal_after": goal_after, "goal_changed": goal_changed, "pre_hash": str(pre_hash),
            "post_hash": str(post_hash), "physical_index": physical_index, "board_uid": board_uid,
            "board_id": board_id, "serial_roundtrip_s": serial, "psetup_s": psetup,
            "ptime_s": ptime, "parity_mode": parity_mode,
            "device_timeout": None, "protocol_retry_count": None,
            "first_round": False, "post_closure": False,
        })
    output.sort(key=lambda row: ((row["start_s"] if row["start_s"] is not None else math.inf), row["call_id"]))
    by_epoch: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for call in output:
        by_epoch[call["epoch_id"]].append(call)
    for epoch_id, calls in by_epoch.items():
        epoch = epoch_by_id.get(epoch_id)
        if epoch is None:
            continue
        expected = [str(value) for value in epoch["expected_robot_ids"]]
        if not expected and epoch["expected_count"] == 1 and calls:
            expected = [calls[0]["robot_id"]]
        elif not expected and epoch["expected_count"] >= 4:
            expected = sorted({call["robot_id"] for call in calls})[:epoch["expected_count"]]
        seen: set[str] = set()
        for call in calls:
            if call["valid"] and call["robot_id"] in expected and call["robot_id"] not in seen:
                call["first_round"] = True
                seen.add(call["robot_id"])
            close = epoch["closed_time_s"]
            if close is not None and call["completion_s"] is not None:
                call["post_closure"] = call["completion_s"] > close + TOLERANCE_S
        epoch["derived_expected_robot_ids"] = expected
    return output


def _normalize_movements(trial: RawTrial, raw: Sequence[Mapping[str, str]]) -> list[dict[str, Any]]:
    hardware = trial.provider == "rp2040_hardware"
    result = []
    for item in raw:
        if hardware:
            robot = str(item.get("logical_robot_id") or "")
            start = _number(item.get("movement_start_s"))
            completion = _number(item.get("movement_completion_s"))
            source = _parse_cell(None, item.get("from_x"), item.get("from_y"))
            target = _parse_cell(None, item.get("to_x"), item.get("to_y"))
            completed = [str(value) for value in _parse_list(item.get("completed_task_ids"))]
        else:
            robot = str(item.get("robot_id") or "")
            start = _number(item.get("start_time_s"))
            completion = _number(item.get("completion_time_s"))
            source = _parse_cell(item.get("source_cell"))
            target = _parse_cell(item.get("target_cell"))
            completed = [str(item["completed_task_id"])] if item.get("completed_task_id") else []
        result.append({
            "movement_id": str(item.get("movement_id") or len(result) + 1), "robot_id": robot,
            "start_s": start, "completion_s": completion, "source": source, "target": target,
            "completed_task_ids": completed,
        })
    result.sort(key=lambda row: ((row["completion_s"] if row["completion_s"] is not None else math.inf), row["movement_id"]))
    return result


def _resolve_trial_manifests(
    trial: RawTrial,
    repository_root: Path,
    raw_root: Path,
    manifest_index: Mapping[str, Path],
) -> tuple[Path, dict[str, Any], Path, dict[str, Any]]:
    scenario_hash = str(trial.publication.get("scenario_sha256") or trial.summary.get("scenario_sha256") or "")
    release_hash = str(trial.publication.get("release_sha256") or trial.summary.get("release_sha256") or "")
    scenario = _resolve_stale_path(trial.summary.get("scenario_manifest"), repository_root, raw_root)
    release = _resolve_stale_path(trial.summary.get("release_manifest"), repository_root, raw_root)
    if scenario is None:
        scenario = manifest_index.get(scenario_hash)
    if release is None:
        release = manifest_index.get(release_hash)
    if scenario is None or release is None:
        # Hardware summary rows do not retain manifest paths.  The hashes are
        # the authoritative lookup keys into the frozen manifest set.
        scenario = scenario or manifest_index.get(scenario_hash)
        release = release or manifest_index.get(release_hash)
    if scenario is None or release is None:
        raise FileNotFoundError(f"cannot resolve scenario/release manifests for {trial.key}")
    scenario_data, scenario_meta = _read_json_hashed(scenario)
    release_data, release_meta = _read_json_hashed(release)
    if scenario_meta["sha256"] != scenario_hash or release_meta["sha256"] != release_hash:
        raise ValueError(f"manifest hash mismatch for {trial.key}")
    return scenario, scenario_data, release, release_data


def _task_source(
    assignment: float | None,
    completion: float | None,
) -> str | None:
    if assignment is None:
        return None
    if completion is not None and abs(assignment - completion) <= TOLERANCE_S:
        return "physical_service_fallback"
    return "allocator"


def _call_for_first_assignment(
    task_cell: tuple[int, int],
    robot: str | None,
    assignment_s: float | None,
    calls: Sequence[Mapping[str, Any]],
) -> Mapping[str, Any] | None:
    if assignment_s is None or robot is None:
        return None
    matches = [
        call for call in calls
        if call["valid"] and call["robot_id"] == robot and call["goal_after"] == task_cell
        and call["completion_s"] is not None
        and abs(float(call["completion_s"]) - assignment_s) <= TOLERANCE_S
    ]
    return matches[0] if matches else None


def _normalize_tasks(
    trial: RawTrial,
    raw: Sequence[Mapping[str, str]],
    scenario: Mapping[str, Any],
    release_manifest: Mapping[str, Any],
    epochs: Sequence[Mapping[str, Any]],
    calls: Sequence[Mapping[str, Any]],
    queue_samples: Sequence[Mapping[str, str]],
) -> list[dict[str, Any]]:
    raw_by_id = {str(item.get("task_id")): item for item in raw}
    scenario_tasks = {str(item["task_id"]): item for item in scenario.get("tasks", [])}
    manifest_tasks = list(release_manifest.get("tasks", []))
    admission_by_task: dict[str, Mapping[str, Any]] = {}
    for epoch in epochs:
        for task_id in epoch["admitted_task_ids"]:
            if task_id in admission_by_task:
                raise ValueError(f"task admitted in multiple epochs: {trial.key} {task_id}")
            admission_by_task[task_id] = epoch
    release_queue: dict[float, tuple[int | None, float | None]] = {}
    hardware = trial.provider == "rp2040_hardware"
    for sample in queue_samples:
        if hardware:
            time_s = _number(sample.get("sample_time_s"))
            event = sample.get("sample_event")
            depth = _integer(sample.get("pending_depth"))
            age = _number(sample.get("oldest_pending_age_s"))
        else:
            time_s = _number(sample.get("time_s"))
            event = sample.get("event")
            depth = _integer(sample.get("depth"))
            age = _number(sample.get("oldest_age_s"))
        if time_s is not None and event == "release":
            release_queue[time_s] = (depth, age)
    planned_releases = [float(item["release_time_s"]) for item in manifest_tasks]
    simultaneous = Counter(planned_releases)
    output: list[dict[str, Any]] = []
    for index, manifest_item in enumerate(manifest_tasks):
        task_id = str(manifest_item["task_id"])
        item = raw_by_id.get(task_id, {})
        scenario_item = scenario_tasks.get(task_id, manifest_item)
        planned_release = float(manifest_item["release_time_s"])
        raw_release = _number(item.get("release_time_s"))
        released = raw_release is not None
        release_s = raw_release if released else planned_release
        admission_s = _number(item.get("admission_time_s"))
        assignment_s = _number(item.get("first_assignment_time_s"))
        completion_s = _number(item.get("completion_time_s"))
        assigned_robot = str(item.get("first_assigned_robot") or "") or None
        completing_robot = str(item.get("completing_robot") or "") or None
        source = _task_source(assignment_s, completion_s)
        epoch = admission_by_task.get(task_id)
        if planned_release <= 0.0:
            raw_reason = "initial_allocation"
            reason = "initial_allocation"
            if epoch is None:
                epoch = next((candidate for candidate in epochs if candidate["initial"]), None)
        else:
            raw_reason = str(epoch["reason_raw"]) if epoch else None
            reason = str(epoch["reason"]) if epoch else None
        possible_call = _call_for_first_assignment(
            (int(scenario_item["x"]), int(scenario_item["y"])),
            assigned_robot, assignment_s, calls,
        )
        # Equal assignment/completion timestamps indicate physical-service
        # fallback unless an allocator call for that robot/task completed at
        # the same timestamp, in which case the allocator path is explicit.
        if possible_call is not None:
            source = "allocator"
        allocator_assignment = assignment_s if source == "allocator" else None
        call = possible_call if source == "allocator" else None
        previous_gap = None if index == 0 else release_s - planned_releases[index - 1]
        next_gap = None if index + 1 == len(planned_releases) else planned_releases[index + 1] - release_s
        queue_depth = release_queue.get(raw_release, (None, None))[0] if raw_release is not None else None
        wait = _number(trial.publication.get("policy_max_pending_age_s"))
        deadline = release_s + wait if wait is not None and release_s > 0 else None
        bounded_compliance = (
            admission_s <= deadline + TOLERANCE_S
            if deadline is not None and admission_s is not None else None
        )
        assignment_events = _integer(item.get("assignment_events"))
        reassignments = _integer(item.get("reassignment_count")) or 0
        if assignment_events is None:
            assignment_events = (1 + reassignments) if assignment_s is not None else 0
        row = {
            "task_id": task_id, "task_x": int(scenario_item["x"]), "task_y": int(scenario_item["y"]),
            "release_order": index + 1, "is_initial_task": planned_release <= 0.0,
            "is_online_task": planned_release > 0.0, "release_time_s": release_s,
            "was_released_by_trial_end": released, "previous_interarrival_s": previous_gap,
            "next_interarrival_s": next_gap, "simultaneous_release_group_size": simultaneous[planned_release],
            "task_state_at_trial_end": item.get("state") or "unreleased",
            "active_task_count_at_release": None, "pending_depth_immediately_after_release": queue_depth,
            "minimum_robot_distance_at_release": None, "admission_time_s": admission_s,
            "admission_epoch_id": epoch["epoch_id"] if epoch else None,
            "raw_admission_reason": raw_reason, "normalized_admission_reason": reason,
            "admitted_by_mandatory_piggyback": epoch["piggyback"] if epoch else None,
            "queue_depth_immediately_before_admission": epoch["pending_before"] if epoch else None,
            "admitted_batch_size": epoch["admitted_count"] if epoch else None,
            "pending_age_at_admission_s": admission_s - release_s if admission_s is not None else None,
            "release_to_admission_latency_s": admission_s - release_s if admission_s is not None else None,
            "bounded_wait_deadline_s": deadline, "bounded_wait_compliance": bounded_compliance,
            "first_any_assignment_time_s": assignment_s,
            "first_allocator_assignment_time_s": allocator_assignment,
            "first_assignment_source": source, "first_assigned_robot": assigned_robot,
            "first_assignment_call_id": call["call_id"] if call else None,
            "first_assignment_epoch_id": call["epoch_id"] if call else None,
            "assignment_event_count": assignment_events,
            "repeated_same_owner_assignment_count": None, "distinct_owner_change_count": None,
            "unique_owner_count": None, "final_assigned_owner": None,
            "release_to_first_any_assignment_latency_s": assignment_s - release_s if assignment_s is not None else None,
            "release_to_first_allocator_assignment_latency_s": allocator_assignment - release_s if allocator_assignment is not None else None,
            "admission_to_first_any_assignment_latency_s": assignment_s - admission_s if assignment_s is not None and admission_s is not None else None,
            "admission_to_first_allocator_assignment_latency_s": allocator_assignment - admission_s if allocator_assignment is not None and admission_s is not None else None,
            "completion_time_s": completion_s, "completion_robot": completing_robot,
            "first_any_assignment_to_completion_latency_s": completion_s - assignment_s if completion_s is not None and assignment_s is not None else None,
            "release_to_completion_latency_s": completion_s - release_s if completion_s is not None else None,
            "mission_ending_task": False, "last_released_task": planned_release == max(planned_releases),
            "allocator_only_decomposition_defined": source == "allocator" and completion_s is not None,
            "reassignment_count": reassignments,
        }
        output.append(row)
    # Reconstruct task-set size at release from lifecycle timestamps.  This is
    # exact at the event clock; simultaneous release/admission events are
    # counted after the timestamp's atomic admission state becomes visible.
    for task in output:
        if not task["was_released_by_trial_end"]:
            continue
        time_s = task["release_time_s"]
        task["active_task_count_at_release"] = sum(
            other["admission_time_s"] is not None
            and other["admission_time_s"] <= time_s + TOLERANCE_S
            and (other["completion_time_s"] is None or other["completion_time_s"] > time_s + TOLERANCE_S)
            for other in output
        )
    completed = [task for task in output if task["completion_time_s"] is not None]
    if completed:
        final = max(completed, key=lambda task: (task["completion_time_s"], task["task_id"]))
        final["mission_ending_task"] = _truth(trial.publication.get("all_tasks_completed")) is True
    return output


def _task_detail_row(trial: RawTrial, task: Mapping[str, Any]) -> dict[str, Any]:
    base = _base_identifiers(trial)
    return {
        **base,
        "arrival_rate_tasks_per_s": ARRIVAL_RATES[trial.publication["arrival_load"]],
        "batch_size": trial.publication.get("batch_size"),
        "maximum_wait_s": trial.publication.get("policy_max_pending_age_s"),
        "trace_id_number": _trace_number(trial.publication["trace_id"]),
        **{field: task.get(field) for field in TASK_FIELDS if field not in base},
    }


def _semantic_events(
    trial: RawTrial,
    tasks: Sequence[Mapping[str, Any]],
    epochs: Sequence[Mapping[str, Any]],
    calls: Sequence[Mapping[str, Any]],
    movements: Sequence[Mapping[str, Any]],
    horizon_s: float | None,
) -> list[dict[str, Any]]:
    events: list[dict[str, Any]] = []
    for task in tasks:
        if task["was_released_by_trial_end"]:
            events.append({"time_s": task["release_time_s"], "source": "task", "kind": "release", "task_id": task["task_id"]})
        if task["admission_time_s"] is not None:
            events.append({
                "time_s": task["admission_time_s"], "source": "task", "kind": "admission",
                "task_id": task["task_id"], "epoch_id": task["admission_epoch_id"],
                "reason": task["normalized_admission_reason"],
            })
        if task["first_any_assignment_time_s"] is not None:
            events.append({
                "time_s": task["first_any_assignment_time_s"], "source": "task", "kind": "first_assignment",
                "task_id": task["task_id"], "robot_id": task["first_assigned_robot"],
                "owner": task["first_assigned_robot"], "reason": task["first_assignment_source"],
                "epoch_id": task["first_assignment_epoch_id"], "call_id": task["first_assignment_call_id"],
            })
        if task["completion_time_s"] is not None:
            events.append({
                "time_s": task["completion_time_s"], "source": "task", "kind": "completion",
                "task_id": task["task_id"], "robot_id": task["completion_robot"],
            })
    for epoch in epochs:
        events.append({
            "time_s": epoch["opened_time_s"], "source": "epoch", "kind": "epoch",
            "epoch_id": epoch["epoch_id"], "reason": epoch["reason"],
            "details_sha256": _hash_sequence(epoch["admitted_task_ids"]),
        })
    for call in calls:
        if not call["valid"]:
            continue
        events.append({
            "time_s": call["completion_s"], "source": "call", "kind": "allocator_call",
            "robot_id": call["robot_id"], "epoch_id": call["epoch_id"], "call_id": call["call_id"],
            "reason": call["call_class"], "goal": call["goal_after"], "state_hash": call["post_hash"],
        })
    for movement in movements:
        events.append({
            "time_s": movement["completion_s"], "source": "movement", "kind": "movement_edge",
            "robot_id": movement["robot_id"], "reason": f"{movement['source']}->{movement['target']}",
            "details_sha256": _hash_sequence([movement["source"], movement["target"]]),
        })
    if _truth(trial.publication.get("all_tasks_completed")) is not True and horizon_s is not None:
        events.append({"time_s": horizon_s, "source": "trial", "kind": "horizon_termination", "reason": trial.publication.get("algorithmic_failure_type")})
    priority = {"release": 0, "admission": 1, "epoch": 2, "allocator_call": 3, "first_assignment": 4, "movement_edge": 5, "completion": 6, "horizon_termination": 7}
    events.sort(key=lambda event: (
        event["time_s"] if event.get("time_s") is not None else math.inf,
        priority.get(str(event.get("kind")), 99), str(event.get("robot_id", "")), str(event.get("task_id", "")),
    ))
    released = admitted = completed = 0
    positions: dict[str, str] = {}
    for index, event in enumerate(events):
        if event["kind"] == "release": released += 1
        if event["kind"] == "admission": admitted += 1
        if event["kind"] == "completion": completed += 1
        if event["kind"] == "movement_edge" and event.get("robot_id"):
            positions[str(event["robot_id"])] = str(event.get("reason", "")).split("->")[-1]
        event["sequence_index"] = index
        event["released_count"] = released
        event["admitted_count"] = admitted
        event["completed_count"] = completed
        event["progress_signature_sha256"] = _hash_sequence([released, admitted, completed, positions])
    return events


ADMISSION_SUMMARY_FIELDS = IDENTIFIER_FIELDS + [
    "normalized_admission_reason", "task_classification", "task_count",
    "release_to_admission_mean_s", "release_to_admission_median_s", "release_to_admission_q90_s",
    "release_to_admission_q95_s", "release_to_admission_max_s",
    "admission_to_first_any_assignment_mean_s", "admission_to_first_any_assignment_median_s",
    "admission_to_first_any_assignment_q90_s", "admission_to_first_any_assignment_q95_s",
    "admission_to_first_any_assignment_max_s", "admission_to_first_allocator_assignment_mean_s",
    "admission_to_first_allocator_assignment_median_s", "admission_to_first_allocator_assignment_q90_s",
    "admission_to_first_allocator_assignment_q95_s", "admission_to_first_allocator_assignment_max_s",
    "release_to_first_any_assignment_mean_s", "release_to_first_any_assignment_median_s",
    "release_to_first_any_assignment_q90_s", "release_to_first_any_assignment_q95_s",
    "release_to_first_any_assignment_max_s", "release_to_first_allocator_assignment_mean_s",
    "release_to_first_allocator_assignment_median_s", "release_to_first_allocator_assignment_q90_s",
    "release_to_first_allocator_assignment_q95_s", "release_to_first_allocator_assignment_max_s",
    "release_to_completion_mean_s", "release_to_completion_median_s", "release_to_completion_q90_s",
    "release_to_completion_q95_s", "release_to_completion_max_s", "fallback_assignment_count",
    "mean_admitted_batch_size", "mean_pending_age_s", "completion_count",
]


def _admission_summary_rows(trial: RawTrial, tasks: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    groups: dict[tuple[str, str], list[Mapping[str, Any]]] = defaultdict(list)
    for task in tasks:
        reason = str(task.get("normalized_admission_reason") or "not_admitted")
        classification = "initial" if task["is_initial_task"] else "online"
        groups[(reason, classification)].append(task)
    result = []
    for (reason, classification), values in sorted(groups.items()):
        row = {**_base_identifiers(trial), "normalized_admission_reason": reason, "task_classification": classification, "task_count": len(values)}
        families = {
            "release_to_admission": "release_to_admission_latency_s",
            "admission_to_first_any_assignment": "admission_to_first_any_assignment_latency_s",
            "admission_to_first_allocator_assignment": "admission_to_first_allocator_assignment_latency_s",
            "release_to_first_any_assignment": "release_to_first_any_assignment_latency_s",
            "release_to_first_allocator_assignment": "release_to_first_allocator_assignment_latency_s",
            "release_to_completion": "release_to_completion_latency_s",
        }
        for prefix, field_name in families.items():
            summary = _stats(item[field_name] for item in values)
            for stat in ("mean", "median", "q90", "q95", "max"):
                row[f"{prefix}_{stat}_s"] = summary[stat]
        row.update({
            "fallback_assignment_count": sum(item["first_assignment_source"] == "physical_service_fallback" for item in values),
            "mean_admitted_batch_size": _stats(item["admitted_batch_size"] for item in values)["mean"],
            "mean_pending_age_s": _stats(item["pending_age_at_admission_s"] for item in values)["mean"],
            "completion_count": sum(item["completion_time_s"] is not None for item in values),
        })
        result.append(row)
    return result


def _epoch_detail_rows(
    trial: RawTrial,
    epochs: Sequence[MutableMapping[str, Any]],
    calls: Sequence[Mapping[str, Any]],
    tasks: Sequence[Mapping[str, Any]],
    movements: Sequence[Mapping[str, Any]],
) -> list[dict[str, Any]]:
    calls_by_epoch: dict[str, list[Mapping[str, Any]]] = defaultdict(list)
    for call in calls:
        if call["epoch_id"]:
            calls_by_epoch[str(call["epoch_id"])].append(call)
    result: list[dict[str, Any]] = []
    for position, epoch in enumerate(epochs):
        associated = calls_by_epoch.get(str(epoch["epoch_id"]), [])
        valid = [call for call in associated if call["valid"]]
        first = [call for call in valid if call["first_round"]]
        start = epoch["opened_time_s"]
        previous_start = epochs[position - 1]["opened_time_s"] if position else None
        next_start = epochs[position + 1]["opened_time_s"] if position + 1 < len(epochs) else None
        admitted_tasks = [task for task in tasks if task["task_id"] in set(epoch["admitted_task_ids"])]
        durations = [call["injected_s"] for call in valid]
        candidates = [call["candidate_count"] for call in valid if call["candidate_count"] is not None]
        calls_by_class = Counter(call["call_class"] for call in valid)
        last_completion = max((call["completion_s"] for call in valid if call["completion_s"] is not None), default=None)
        span_starts = [call["start_s"] for call in valid if call["start_s"] is not None]
        span_ends = [call["completion_s"] for call in valid if call["completion_s"] is not None]
        first_starts = [call["start_s"] for call in first if call["start_s"] is not None]
        first_ends = [call["completion_s"] for call in first if call["completion_s"] is not None]
        maximum_pending = max((task["pending_age_at_admission_s"] for task in admitted_tasks if task["pending_age_at_admission_s"] is not None), default=None)
        wait = _number(trial.publication.get("policy_max_pending_age_s"))
        deadline = (start - epoch["oldest_pending_age_s"] + wait) if wait is not None and epoch["admitted_count"] else None
        expected = epoch.get("derived_expected_robot_ids") or epoch["expected_robot_ids"]
        global_local = "global" if len(expected) >= 4 or epoch["admitted_count"] > 0 else "local"
        active_at_start = sum(
            task["admission_time_s"] is not None and task["admission_time_s"] <= start + TOLERANCE_S
            and (task["completion_time_s"] is None or task["completion_time_s"] > start + TOLERANCE_S)
            for task in tasks
        ) if start is not None else None
        interval_left = previous_start if previous_start is not None else -math.inf
        completed_since = sum(
            task["completion_time_s"] is not None and interval_left < task["completion_time_s"] <= start + TOLERANCE_S
            for task in tasks
        ) if start is not None else None
        steps_since = sum(
            movement["completion_s"] is not None and interval_left < movement["completion_s"] <= start + TOLERANCE_S
            for movement in movements
        ) if start is not None else None
        row = {
            **_base_identifiers(trial), "epoch_id": epoch["epoch_id"],
            "virtual_epoch_start_time_s": start, "epoch_close_time_s": epoch["closed_time_s"],
            "last_associated_call_completion_time_s": last_completion, "raw_epoch_reason": epoch["reason_raw"],
            "normalized_epoch_reason": epoch["reason"], "mandatory": epoch["mandatory"], "initial": epoch["initial"],
            "global_or_local": global_local, "initiating_robot": expected[0] if global_local == "local" and expected else None,
            "admission": epoch["admitted_count"] > 0, "admitted_task_count": epoch["admitted_count"],
            "admitted_task_ids_sha256": _hash_sequence(epoch["admitted_task_ids"]),
            "pending_depth_before_admission": epoch["pending_before"],
            "pending_depth_after_admission": 0 if epoch["admitted_count"] else epoch["pending_before"],
            "oldest_pending_age_before_admission_s": epoch["oldest_pending_age_s"],
            "maximum_admitted_task_pending_age_s": maximum_pending,
            "batch_threshold": trial.publication.get("batch_size"), "timeout_deadline_s": deadline,
            "timeout_lateness_s": start - deadline if deadline is not None and epoch["reason"] == "age_timeout" else None,
            "piggyback": epoch["piggyback"], "expected_first_round_robot_count": len(expected),
            "observed_first_round_robot_count": len(first), "total_associated_calls": len(valid),
            "post_first_round_calls": sum(not call["first_round"] for call in valid),
            "post_closure_calls": sum(call["post_closure"] for call in valid),
            "unique_robots_called": len({call["robot_id"] for call in valid}),
            "calls_by_normalized_class": _counter_json(calls_by_class),
            "agx_call_work_sum_s": sum(call["agx_total_s"] for call in valid),
            "rp2040_call_work_sum_s": sum(call["rp_choose_s"] + call["rp_reset_s"] for call in valid),
            "injected_call_work_sum_s": sum(call["injected_s"] for call in valid),
            "first_round_compute_makespan_s": max(first_ends) - min(first_starts) if first_starts and first_ends else None,
            "total_associated_call_span_s": max(span_ends) - min(span_starts) if span_starts and span_ends else None,
            "per_call_duration_mean_s": _stats(durations)["mean"],
            "per_call_duration_median_s": _stats(durations)["median"],
            "per_call_duration_max_s": _stats(durations)["max"],
            "active_task_count_at_epoch_start": active_at_start,
            "candidate_count_mean": _stats(candidates)["mean"], "candidate_count_max": max(candidates) if candidates else None,
            "inbound_message_count": None, "outbound_message_count": None,
            "goal_change_count": sum(call["goal_changed"] for call in valid),
            "assignment_event_count": sum(
                task["first_assignment_epoch_id"] == epoch["epoch_id"] for task in tasks
            ),
            "owner_change_count": None, "completed_tasks_since_previous_epoch": completed_since,
            "robot_steps_since_previous_epoch": steps_since,
            "time_since_previous_epoch_s": start - previous_start if start is not None and previous_start is not None else None,
            "time_to_next_epoch_s": next_start - start if start is not None and next_start is not None else None,
            "epoch_close_semantics": epoch["closure_semantics"],
            "expected_robot_set_derivation": epoch["expected_derivation"],
        }
        result.append(row)
    return result


def _call_summary_rows(trial: RawTrial, calls: Sequence[Mapping[str, Any]], tasks: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    groups: dict[tuple[str, str, str, str, str], list[Mapping[str, Any]]] = defaultdict(list)
    for call in calls:
        groups[(
            call["robot_id"], call["parent_reason"], call["call_class"],
            "first_round" if call["first_round"] else "post_first_round",
            "post_closure" if call["post_closure"] else "not_post_closure",
        )].append(call)
    rows = []
    for key, values in sorted(groups.items()):
        durations = [call["injected_s"] for call in values if call["valid"]]
        candidates = [call["candidate_count"] for call in values if call["candidate_count"] is not None]
        active = [call["active_count"] for call in values if call["active_count"] is not None]
        goal_changes = sum(call["goal_changed"] for call in values if call["valid"])
        epoch_ids = {call["epoch_id"] for call in values}
        call_ids = {call["call_id"] for call in values}
        assignment_events = sum(task["first_assignment_call_id"] in call_ids for task in tasks)
        rows.append({
            **_base_identifiers(trial), "robot_id": key[0], "normalized_epoch_reason": key[1],
            "normalized_call_class": key[2], "first_round_status": key[3], "post_closure_status": key[4],
            "call_count": len(values), "agx_work_sum_s": sum(call["agx_total_s"] for call in values if call["valid"]),
            "rp2040_work_sum_s": sum(call["rp_choose_s"] + call["rp_reset_s"] for call in values if call["valid"]),
            "injected_work_sum_s": sum(call["injected_s"] for call in values if call["valid"]),
            "call_duration_mean_s": _stats(durations)["mean"], "call_duration_median_s": _stats(durations)["median"],
            "call_duration_q90_s": _stats(durations)["q90"], "call_duration_q95_s": _stats(durations)["q95"],
            "call_duration_max_s": _stats(durations)["max"], "candidate_count_mean": _stats(candidates)["mean"],
            "candidate_count_median": _stats(candidates)["median"], "candidate_count_q90": _stats(candidates)["q90"],
            "candidate_count_max": _stats(candidates)["max"], "active_task_count_mean": _stats(active)["mean"],
            "active_task_count_median": _stats(active)["median"], "active_task_count_max": _stats(active)["max"],
            "inbound_message_count": None, "outbound_message_count": None,
            "goal_change_count": goal_changes, "goal_change_rate": goal_changes / len(values) if values else None,
            "assignment_event_count": assignment_events, "parity_failure_count": sum(call["parity"] is False for call in values),
            "buffered_event_count": None,
        })
    return rows


def _concurrency_diagnostics(calls: Sequence[Mapping[str, Any]], horizon: float | None) -> dict[str, Any]:
    valid_intervals = [
        (float(call["start_s"]), float(call["completion_s"]), str(call["robot_id"]))
        for call in calls if call["valid"] and call["start_s"] is not None and call["completion_s"] is not None
    ]
    union_s, merged = _interval_union((start, end) for start, end, _ in valid_intervals)
    by_robot: dict[str, list[tuple[float, float]]] = defaultdict(list)
    for start, end, robot in valid_intervals:
        by_robot[robot].append((start, end))
    robot_busy = {robot: _interval_union(intervals)[0] for robot, intervals in by_robot.items()}
    for robot in ("00", "01", "02", "03"):
        robot_busy.setdefault(robot, 0.0)
    sweep = []
    for start, end, _ in valid_intervals:
        sweep.append((start, 1))
        sweep.append((end, -1))
    sweep.sort(key=lambda item: (item[0], item[1]))
    current = maximum = 0
    for _, delta in sweep:
        current += delta
        maximum = max(maximum, current)
    fractions = [busy / horizon for busy in robot_busy.values()] if horizon and horizon > 0 else []
    return {
        "virtual_compute_union_diagnostic_s": union_s,
        "summed_per_robot_compute_union_diagnostic_s": sum(robot_busy.values()),
        "mean_per_robot_compute_busy_diagnostic_s": statistics.fmean(robot_busy.values()),
        "max_per_robot_compute_busy_diagnostic_s": max(robot_busy.values(), default=0.0),
        "mean_per_robot_compute_busy_fraction_diagnostic": statistics.fmean(fractions) if fractions else None,
        "max_per_robot_compute_busy_fraction_diagnostic": max(fractions) if fractions else None,
        "max_simultaneous_logical_compute_count_diagnostic": maximum,
        "mean_logical_compute_concurrency_while_active_diagnostic": (
            sum(end - start for start, end, _ in valid_intervals) / union_s if union_s > 0 else 0.0
        ),
        "global_intervals": merged, "robot_intervals": by_robot,
    }


def _trial_summary_row(
    trial: RawTrial,
    tasks: Sequence[Mapping[str, Any]],
    epochs: Sequence[Mapping[str, Any]],
    calls: Sequence[Mapping[str, Any]],
    movements: Sequence[Mapping[str, Any]],
    semantic: Sequence[Mapping[str, Any]],
    release_manifest: Mapping[str, Any],
    scenario: Mapping[str, Any],
) -> dict[str, Any]:
    row: dict[str, Any] = dict(trial.publication)
    derived: dict[str, Any] = {field: None for field in TRIAL_APPEND_FIELDS}
    valid_calls = [call for call in calls if call["valid"]]
    invalid_calls = [call for call in calls if not call["valid"]]
    completed = [task for task in tasks if task["completion_time_s"] is not None]
    released = [task for task in tasks if task["was_released_by_trial_end"]]
    admitted = [task for task in tasks if task["admission_time_s"] is not None]
    assigned = [task for task in tasks if task["first_any_assignment_time_s"] is not None]
    online = [task for task in tasks if task["is_online_task"]]
    planned = [float(item["release_time_s"]) for item in release_manifest.get("tasks", [])]
    horizon = _number(trial.publication.get("mission_elapsed_time_s"))
    if horizon is None:
        horizon = _number(trial.publication.get("algorithmic_horizon_time_s")) or _number(trial.summary.get("simulated_execution_time_s"))
    concurrency = _concurrency_diagnostics(calls, horizon)
    admission_epochs = [epoch for epoch in epochs if epoch["admitted_count"] > 0]
    mandatory_epochs = [epoch for epoch in epochs if epoch["mandatory"] and not epoch["initial"]]
    online_scheduler = [task for task in online if task["normalized_admission_reason"] in ARRIVAL_REASONS]
    scopes = {
        "all": list(tasks), "initial": [task for task in tasks if task["is_initial_task"]],
        "online": online, "online_scheduler_admitted": online_scheduler,
    }
    family_fields = {
        "release_to_admission": "release_to_admission_latency_s",
        "admission_to_first_any_assignment": "admission_to_first_any_assignment_latency_s",
        "admission_to_first_allocator_assignment": "admission_to_first_allocator_assignment_latency_s",
        "release_to_first_any_assignment": "release_to_first_any_assignment_latency_s",
        "release_to_first_allocator_assignment": "release_to_first_allocator_assignment_latency_s",
        "first_any_assignment_to_completion": "first_any_assignment_to_completion_latency_s",
        "release_to_completion": "release_to_completion_latency_s",
    }
    for scope, scope_tasks in scopes.items():
        for family, field_name in family_fields.items():
            _add_stats(derived, f"{scope}_{family}", (task[field_name] for task in scope_tasks))
    released_online_times = [task["release_time_s"] for task in online]
    mission_s = _number(trial.publication.get("mission_elapsed_time_s"))
    derived.update({
        "arrival_rate_tasks_per_s": ARRIVAL_RATES[trial.publication["arrival_load"]],
        "mean_interarrival_s": 1.0 / ARRIVAL_RATES[trial.publication["arrival_load"]],
        "initial_task_count": sum(task["is_initial_task"] for task in tasks),
        "online_task_count": sum(task["is_online_task"] for task in tasks),
        "first_online_release_time_s": min(released_online_times) if released_online_times else None,
        "last_release_time_s": max(planned) if planned else None,
        "released_task_count_at_end": len(released), "admitted_task_count_at_end": len(admitted),
        "assigned_task_count_at_end": len(assigned),
        "allocator_assigned_task_count_at_end": sum(task["first_assignment_source"] == "allocator" for task in assigned),
        "service_fallback_assigned_task_count": sum(task["first_assignment_source"] == "physical_service_fallback" for task in assigned),
        "completed_task_count_at_end": len(completed),
        "next_release_after_horizon_s": min((task["release_time_s"] for task in tasks if not task["was_released_by_trial_end"]), default=None),
        "mission_elapsed_minus_last_release_s": mission_s - max(planned) if mission_s is not None and planned else None,
        "online_admission_to_assignment_mean_s": _stats(task["admission_to_first_any_assignment_latency_s"] for task in online)["mean"],
        "online_assignment_to_completion_mean_s": _stats(task["first_any_assignment_to_completion_latency_s"] for task in online)["mean"],
        "online_release_to_first_assignment_mean_s": _stats(task["release_to_first_any_assignment_latency_s"] for task in online)["mean"],
        "admission_epoch_count": len(admission_epochs),
        "initial_allocation_admitted_task_count": sum(epoch["admitted_count"] for epoch in epochs if epoch["reason"] == "initial_allocation"),
        "eager_arrival_admitted_task_count": sum(epoch["admitted_count"] for epoch in epochs if epoch["reason"] == "task_arrival_eager"),
        "batch_threshold_admitted_task_count": sum(epoch["admitted_count"] for epoch in epochs if epoch["reason"] == "batch_threshold"),
        "age_timeout_admitted_task_count": sum(epoch["admitted_count"] for epoch in epochs if epoch["reason"] == "age_timeout"),
        "final_release_flush_admitted_task_count": sum(epoch["admitted_count"] for epoch in epochs if epoch["reason"] == "final_release_flush"),
        "mandatory_piggyback_admitted_task_count": sum(epoch["admitted_count"] for epoch in epochs if epoch["piggyback"]),
        "bounded_timeout_admission_count": sum(epoch["reason"] == "age_timeout" for epoch in epochs),
        "bounded_tasks_admitted_at_approximately_w5_count": sum(
            abs(task["release_to_admission_latency_s"] - 5.0) <= TOLERANCE_S
            for task in online if task["release_to_admission_latency_s"] is not None and trial.publication["policy_id"] == "bounded_b4_w5"
        ),
        "bounded_pending_age_violation_count": sum(
            task["release_to_admission_latency_s"] > 5.0 + TOLERANCE_S
            for task in online if task["release_to_admission_latency_s"] is not None and trial.publication["policy_id"] == "bounded_b4_w5"
        ),
        "final_partial_batch_flush_count": sum(epoch["reason"] == "final_release_flush" for epoch in epochs),
        "mandatory_piggyback_admission_count": sum(epoch["piggyback"] for epoch in epochs),
        "task_weighted_vs_event_sampled_queue_metrics_distinct": True,
        "total_epoch_count": len(epochs), "initial_epoch_count": sum(epoch["initial"] for epoch in epochs),
        "mandatory_epoch_count": len(mandatory_epochs),
        "mandatory_task_completion_epoch_count": sum(epoch["reason"] == "task_completion" for epoch in mandatory_epochs),
        "mandatory_invalid_goal_epoch_count": sum(epoch["reason"] == "invalid_goal" for epoch in mandatory_epochs),
        "mandatory_robot_idle_epoch_count": sum(epoch["reason"] == "robot_idle" for epoch in mandatory_epochs),
        "local_epoch_count": sum(len(epoch.get("derived_expected_robot_ids") or epoch["expected_robot_ids"]) < 4 for epoch in epochs),
        "global_epoch_count": sum(len(epoch.get("derived_expected_robot_ids") or epoch["expected_robot_ids"]) >= 4 for epoch in epochs),
        "total_valid_allocator_call_count": len(valid_calls), "invalid_or_parity_rejected_call_count": len(invalid_calls),
        "calls_per_epoch": len(valid_calls) / len(epochs) if epochs else None,
        "calls_per_admission_epoch": sum(call["parent_reason"] in ARRIVAL_REASONS or call["parent_reason"] == "initial_allocation" for call in valid_calls) / len(admission_epochs) if admission_epochs else None,
        "calls_per_mandatory_epoch": sum(call["parent_reason"] in MANDATORY_REASONS for call in valid_calls) / len(mandatory_epochs) if mandatory_epochs else None,
        "calls_per_admitted_online_task": len(valid_calls) / sum(task["admission_time_s"] is not None for task in online) if any(task["admission_time_s"] is not None for task in online) else None,
        "expected_first_round_call_count": sum(len(epoch.get("derived_expected_robot_ids") or epoch["expected_robot_ids"]) for epoch in epochs),
        "observed_first_round_call_count": sum(call["first_round"] for call in valid_calls),
        "post_first_round_call_count": sum(not call["first_round"] for call in valid_calls),
        "post_closure_call_count": sum(call["post_closure"] for call in valid_calls),
        "calls_associated_with_admission_epochs": sum(call["parent_reason"] in ARRIVAL_REASONS or call["parent_reason"] == "initial_allocation" for call in valid_calls),
        "calls_associated_with_mandatory_epochs": sum(call["parent_reason"] in MANDATORY_REASONS for call in valid_calls),
        "calls_with_current_goal_count": sum(call["goal_before"] is not None for call in valid_calls),
        "calls_with_no_current_goal_count": sum(call["goal_before"] is None for call in valid_calls),
        "calls_that_changed_goal_count": sum(call["goal_changed"] for call in valid_calls),
        "calls_that_retained_goal_count": sum(not call["goal_changed"] for call in valid_calls),
        "unique_robots_invoked_per_epoch_mean": _stats(len(set(epoch["called_robot_ids_raw"])) for epoch in epochs)["mean"],
        "unique_robots_invoked_per_epoch_max": max((len(set(epoch["called_robot_ids_raw"])) for epoch in epochs), default=None),
        "agx_choose_goal_work_s": sum(call["agx_choose_s"] for call in valid_calls),
        "agx_epoch_reset_work_s": sum(call["agx_reset_s"] for call in valid_calls),
        "total_agx_allocator_work_s": sum(call["agx_total_s"] for call in valid_calls),
        "rp2040_choose_goal_work_s": sum(call["rp_choose_s"] for call in valid_calls),
        "rp2040_epoch_reset_work_s": sum(call["rp_reset_s"] for call in valid_calls),
        "total_rp2040_allocator_work_s": sum(call["rp_choose_s"] + call["rp_reset_s"] for call in valid_calls),
        "injected_provider_work_s": sum(call["injected_s"] for call in valid_calls),
        "zero_compute_injected_work_s": sum(call["injected_s"] for call in valid_calls) if trial.dataset == "zero_compute" else 0.0,
        "provider_allocator_processor_work_s": (
            0.0 if trial.provider == "zero_compute" else
            sum(call["rp_choose_s"] + call["rp_reset_s"] for call in valid_calls) if trial.provider == "rp2040_hardware" else
            sum(call["agx_total_s"] for call in valid_calls)
        ),
        "provider_allocator_processor_work_definition": (
            "zero injected duration; AGX diagnostic work separate" if trial.provider == "zero_compute" else
            "sum of RP2040 choose_goal and epoch-reset device work across logical robots" if trial.provider == "rp2040_hardware" else
            "sum of authoritative AGX choose_goal and epoch-reset processor work across logical robots"
        ),
        "summed_valid_call_duration_s": sum(call["injected_s"] for call in valid_calls),
        "release_events_during_virtual_compute_count": sum(
            task["was_released_by_trial_end"] and _contains_time(concurrency["global_intervals"], task["release_time_s"])
            for task in online
        ),
        "admission_epochs_during_virtual_compute_count": sum(
            epoch["opened_time_s"] is not None and _contains_time(concurrency["global_intervals"], epoch["opened_time_s"])
            for epoch in admission_epochs
        ),
        "buffered_message_count": None, "maximum_compute_buffering_delay_s": None,
        "total_assignment_event_count": sum(task["assignment_event_count"] for task in tasks),
        "first_assignment_count": len(assigned), "repeated_same_owner_assignment_event_count": None,
        "distinct_owner_change_count": None, "total_task_reassignment_count": sum(task["reassignment_count"] for task in tasks),
        "mean_unique_owners_per_task": None, "max_unique_owners_per_task": None,
        "task_goal_change_count": None, "robot_goal_change_count": sum(call["goal_changed"] for call in valid_calls),
        "bundle_reset_count": None,
        "assignment_churn_per_completed_task": sum(task["reassignment_count"] for task in tasks) / len(completed) if completed else None,
        "reconstructed_total_team_steps": len(movements),
        "reconstructed_max_robot_steps": max(Counter(movement["robot_id"] for movement in movements).values(), default=0),
        "turn_count": None, "replan_count": None, "blocked_intent_collision_count": None,
        "robot_idle_event_count": sum(epoch["reason"] == "robot_idle" for epoch in epochs),
        "invalid_goal_event_count": sum(epoch["reason"] == "invalid_goal" for epoch in epochs),
        "quarantine_count": None, "final_task_timing_is_formal_critical_path": False,
        "algorithmic_horizon_type": trial.publication.get("algorithmic_failure_type") if not completed or len(completed) < 50 else None,
        "horizon_time_s": horizon if _truth(trial.publication.get("all_tasks_completed")) is not True else None,
        "total_processed_event_count": None, "stagnation_event_count": None,
        "released_count_at_stop": len(released), "admitted_count_at_stop": len(admitted),
        "assigned_count_at_stop": len(assigned), "completed_count_at_stop": len(completed),
        "next_scheduled_release_after_stop_s": min((task["release_time_s"] for task in tasks if not task["was_released_by_trial_end"]), default=None),
        "final_active_task_count": sum(task["admission_time_s"] is not None and task["completion_time_s"] is None for task in tasks),
        "final_pending_task_count": sum(task["was_released_by_trial_end"] and task["admission_time_s"] is None for task in tasks),
    })
    for key in (
        "virtual_compute_union_diagnostic_s", "summed_per_robot_compute_union_diagnostic_s",
        "mean_per_robot_compute_busy_diagnostic_s", "max_per_robot_compute_busy_diagnostic_s",
        "mean_per_robot_compute_busy_fraction_diagnostic", "max_per_robot_compute_busy_fraction_diagnostic",
        "max_simultaneous_logical_compute_count_diagnostic", "mean_logical_compute_concurrency_while_active_diagnostic",
    ):
        derived[key] = concurrency[key]
    call_duration_stats = _stats(call["injected_s"] for call in valid_calls)
    for stat in ("median", "q90", "q95", "max"):
        derived[f"per_call_duration_{stat}_s"] = call_duration_stats[stat]
    ratios = [
        (call["rp_choose_s"] + call["rp_reset_s"]) / call["agx_total_s"]
        for call in valid_calls if trial.provider == "rp2040_hardware" and call["agx_total_s"] > 0
    ]
    derived["median_per_call_rp2040_to_agx_ratio"] = _stats(ratios)["median"]
    for value in CALL_CLASSES:
        selected = [call for call in valid_calls if call["call_class"] == value]
        derived[f"call_class_{value}_count"] = len(selected)
        derived[f"call_class_{value}_work_s"] = sum(call["injected_s"] for call in selected)
    for value in EPOCH_REASONS:
        selected = [call for call in valid_calls if call["parent_reason"] == value]
        derived[f"parent_epoch_reason_{value}_call_count"] = len(selected)
        derived[f"parent_epoch_reason_{value}_work_s"] = sum(call["injected_s"] for call in selected)
    batch_stats = _stats(epoch["admitted_count"] for epoch in admission_epochs)
    age_stats = _stats(epoch["oldest_pending_age_s"] for epoch in admission_epochs)
    online_wait_stats = _stats(task["release_to_admission_latency_s"] for task in online)
    for stat in ("mean", "median", "q90", "max"):
        derived[f"admitted_batch_size_{stat}"] = batch_stats[stat]
        derived[f"event_sampled_pending_age_at_admission_{stat}_s"] = age_stats[stat]
        derived[f"online_release_to_admission_task_weighted_{stat}_s"] = online_wait_stats[stat]
    decomp_tasks = [task for task in online if task["release_to_completion_latency_s"] is not None and task["admission_to_first_any_assignment_latency_s"] is not None]
    total_service = sum(task["release_to_completion_latency_s"] for task in decomp_tasks)
    if total_service > 0:
        derived["online_service_latency_release_to_admission_fraction"] = sum(task["release_to_admission_latency_s"] for task in decomp_tasks) / total_service
        derived["online_service_latency_admission_to_assignment_fraction"] = sum(task["admission_to_first_any_assignment_latency_s"] for task in decomp_tasks) / total_service
        derived["online_service_latency_assignment_to_completion_fraction"] = sum(task["first_any_assignment_to_completion_latency_s"] for task in decomp_tasks) / total_service
    derived["online_service_latency_fraction_method"] = "ratio_of_component_sums_over_online_tasks_with_complete_any-source_decomposition"
    final = max(completed, key=lambda task: (task["completion_time_s"], task["task_id"])) if completed else None
    if final and _truth(trial.publication.get("all_tasks_completed")) is True:
        intervals_by_robot = concurrency["robot_intervals"]
        robot_intervals = intervals_by_robot.get(str(final["completion_robot"]), [])
        global_intervals = concurrency["global_intervals"]
        derived.update({
            "final_completed_task_id": final["task_id"], "final_completed_task_cell": [final["task_x"], final["task_y"]],
            "final_completion_robot": final["completion_robot"], "final_task_release_time_s": final["release_time_s"],
            "final_task_admission_time_s": final["admission_time_s"], "final_task_first_assignment_time_s": final["first_any_assignment_time_s"],
            "final_task_first_assignment_source": final["first_assignment_source"], "final_task_completion_time_s": final["completion_time_s"],
            "final_task_was_last_released": final["last_released_task"], "final_task_release_to_admission_s": final["release_to_admission_latency_s"],
            "final_task_admission_to_assignment_s": final["admission_to_first_any_assignment_latency_s"],
            "final_task_release_to_assignment_s": final["release_to_first_any_assignment_latency_s"],
            "final_task_assignment_to_completion_s": final["first_any_assignment_to_completion_latency_s"],
            "final_task_completing_robot_compute_busy_release_to_admission_s": _interval_intersection_s(robot_intervals, final["release_time_s"], final["admission_time_s"]),
            "final_task_completing_robot_compute_busy_admission_to_assignment_s": _interval_intersection_s(robot_intervals, final["admission_time_s"], final["first_any_assignment_time_s"]),
            "final_task_completing_robot_compute_busy_assignment_to_completion_s": _interval_intersection_s(robot_intervals, final["first_any_assignment_time_s"], final["completion_time_s"]),
            "final_task_global_compute_union_release_to_admission_s": _interval_intersection_s(global_intervals, final["release_time_s"], final["admission_time_s"]),
            "final_task_global_compute_union_admission_to_assignment_s": _interval_intersection_s(global_intervals, final["admission_time_s"], final["first_any_assignment_time_s"]),
            "final_task_global_compute_union_assignment_to_completion_s": _interval_intersection_s(global_intervals, final["first_any_assignment_time_s"], final["completion_time_s"]),
        })
    last_progress = max(
        [task["completion_time_s"] for task in tasks if task["completion_time_s"] is not None]
        + [movement["completion_s"] for movement in movements if movement["completion_s"] is not None],
        default=None,
    )
    derived["last_progress_timestamp_s"] = last_progress
    derived["elapsed_since_last_progress_s"] = horizon - last_progress if horizon is not None and last_progress is not None else None
    derived["calls_since_last_progress"] = sum(call["completion_s"] is not None and last_progress is not None and call["completion_s"] > last_progress + TOLERANCE_S for call in valid_calls)
    derived["epochs_since_last_progress"] = sum(epoch["opened_time_s"] is not None and last_progress is not None and epoch["opened_time_s"] > last_progress + TOLERANCE_S for epoch in epochs)
    final_calls = [event for event in semantic[-1000:] if event["source"] == "call"]
    final_epochs = [event for event in semantic[-1000:] if event["source"] == "epoch"]
    derived["dominant_final_window_call_class"] = _dominant(Counter(event["reason"] for event in final_calls))
    derived["dominant_final_window_epoch_reason"] = _dominant(Counter(event["reason"] for event in final_epochs))
    state_counts = Counter(call["post_hash"] for call in valid_calls[-1000:] if call["post_hash"])
    derived["repeated_final_state_signature_count"] = max(state_counts.values(), default=0)
    starts = {str(item["robot_id"]): [int(item["x"]), int(item["y"])] for item in scenario.get("robot_starts", [])}
    for movement in movements:
        if movement["target"] is not None:
            starts[movement["robot_id"]] = list(movement["target"])
    derived["final_robot_positions"] = starts
    for window in (500, 1000):
        tail = semantic[-window:]
        derived[f"final_{window}_semantic_event_call_classes"] = _counter_json(Counter(event["reason"] for event in tail if event["source"] == "call"))
        derived[f"final_{window}_semantic_event_epoch_reasons"] = _counter_json(Counter(event["reason"] for event in tail if event["source"] == "epoch"))
    row.update(derived)
    return row


def _hardware_call_row(trial: RawTrial, call: Mapping[str, Any], epoch_map: Mapping[str, Mapping[str, Any]]) -> dict[str, Any]:
    epoch = epoch_map.get(str(call["epoch_id"]))
    agx = call["agx_total_s"]
    rp = call["rp_choose_s"] + call["rp_reset_s"]
    return {
        **_base_identifiers(trial), "board_uid": call["board_uid"], "robot_context_id": call["context_id"],
        "call_id": call["call_id"], "parent_epoch_id": call["epoch_id"], "epoch_reason": call["parent_reason"],
        "call_class": call["call_class"], "first_round": call["first_round"], "post_closure": call["post_closure"],
        "physical_measurement_sequence_number": call["physical_index"], "virtual_request_time_s": call["start_s"],
        "virtual_start_time_s": call["start_s"], "virtual_completion_time_s": call["completion_s"],
        "virtual_queue_wait_s": 0.0 if call["start_s"] is not None else None, "injected_duration_s": call["injected_s"],
        "agx_authoritative_choose_goal_duration_s": call["agx_choose_s"], "agx_epoch_reset_duration_s": call["agx_reset_s"],
        "rp2040_choose_goal_duration_s": call["rp_choose_s"], "rp2040_epoch_reset_duration_s": call["rp_reset_s"],
        "total_rp2040_device_work_s": rp, "rp2040_to_agx_ratio": rp / agx if agx > 0 else None,
        "physical_host_wall_measurement_start_s": None, "physical_host_wall_measurement_end_s": None,
        "psetup_duration_s": call["psetup_s"], "ptime_duration_s": call["ptime_s"],
        "result_serialization_duration_s": None, "total_serial_round_trip_s": call["serial_roundtrip_s"],
        "protocol_retry_count": call["protocol_retry_count"], "device_timeout": call["device_timeout"],
        "active_task_count": call["active_count"], "candidate_count": call["candidate_count"],
        "admitted_batch_size": epoch["admitted_count"] if epoch else None,
        "goal_before": call["goal_before"], "goal_after": call["goal_after"], "goal_change": call["goal_changed"],
        "inbound_message_count": None, "outbound_message_count": None, "pre_state_hash": call["pre_hash"],
        "post_state_hash": call["post_hash"], "output_parity_result": call["parity"],
        "parity_path_or_comparison_mode": call["parity_mode"], "validity_for_mission": call["valid"],
        "simultaneous_hardware_measurement_count": None,
        "overlapping_agx_authoritative_measurement_count": None,
        "worker_concurrency_at_measurement_start": None, "physical_concurrency_evidence_available": False,
    }


def _bin_count(value: int | None) -> str:
    if value is None:
        return "missing"
    if value <= 7:
        return "0-7"
    if value <= 15:
        return "8-15"
    if value <= 31:
        return "16-31"
    return "32-50"


def _bin_batch(value: int | None) -> str:
    if value is None:
        return "missing"
    if value == 0:
        return "0"
    if value == 1:
        return "1"
    if value <= 3:
        return "2-3"
    if value == 4:
        return "4"
    if value <= 8:
        return "5-8"
    return "9+"


def _file_meta(path: Path | None) -> dict[str, Any]:
    if path is None or not path.is_file():
        return {"path": None, "size": None, "sha256": None}
    return {"path": str(path.resolve()), "size": path.stat().st_size, "sha256": _sha256(path)}


RAW_INDEX_FIELDS = [
    *IDENTIFIER_FIELDS, "source_campaign_root", "raw_trial_directory", "scenario_manifest_path",
    "scenario_manifest_sha256", "release_manifest_path", "release_manifest_sha256",
    "trial_summary_path", "trial_summary_size_bytes", "trial_summary_sha256",
    "task_event_path", "task_event_size_bytes", "task_event_sha256",
    "allocator_call_path", "allocator_call_size_bytes", "allocator_call_sha256",
    "epoch_artifact_raw_filename", "epoch_artifact_path", "epoch_artifact_size_bytes", "epoch_artifact_sha256",
    "movement_event_path", "movement_event_size_bytes", "movement_event_sha256",
    "algorithmic_outcome_or_failure_path", "algorithmic_outcome_or_failure_sha256",
    "retained_attempt_identifier", "historical_failed_technical_attempt_count",
    "trial_summary_complete", "task_artifact_complete", "call_artifact_complete",
    "epoch_artifact_complete", "movement_artifact_complete", "all_required_raw_artifacts_complete",
]


def _raw_index_row(
    trial: RawTrial,
    scenario_path: Path,
    release_path: Path,
    metas: Mapping[str, Mapping[str, Any]],
    epoch_filename: str,
) -> dict[str, Any]:
    failure = trial.artifact_dir / "failure.json"
    outcome = failure if failure.is_file() else None
    complete_flags = {
        "trial_summary_complete": bool(metas["summary"].get("sha256")),
        "task_artifact_complete": bool(metas["tasks"].get("sha256")),
        "call_artifact_complete": bool(metas["calls"].get("sha256")),
        "epoch_artifact_complete": bool(metas["epochs"].get("sha256")),
        "movement_artifact_complete": bool(metas["movements"].get("sha256")),
    }
    return {
        **_base_identifiers(trial), "source_campaign_root": str(trial.campaign_root),
        "raw_trial_directory": str(trial.artifact_dir), "scenario_manifest_path": str(scenario_path),
        "scenario_manifest_sha256": _sha256(scenario_path), "release_manifest_path": str(release_path),
        "release_manifest_sha256": _sha256(release_path),
        "trial_summary_path": metas["summary"]["path"], "trial_summary_size_bytes": metas["summary"]["size"],
        "trial_summary_sha256": metas["summary"]["sha256"],
        "task_event_path": metas["tasks"]["path"], "task_event_size_bytes": metas["tasks"]["size"],
        "task_event_sha256": metas["tasks"]["sha256"], "allocator_call_path": metas["calls"]["path"],
        "allocator_call_size_bytes": metas["calls"]["size"], "allocator_call_sha256": metas["calls"]["sha256"],
        "epoch_artifact_raw_filename": epoch_filename, "epoch_artifact_path": metas["epochs"]["path"],
        "epoch_artifact_size_bytes": metas["epochs"]["size"], "epoch_artifact_sha256": metas["epochs"]["sha256"],
        "movement_event_path": metas["movements"]["path"], "movement_event_size_bytes": metas["movements"]["size"],
        "movement_event_sha256": metas["movements"]["sha256"],
        "algorithmic_outcome_or_failure_path": str(outcome.resolve()) if outcome else None,
        "algorithmic_outcome_or_failure_sha256": _sha256(outcome) if outcome else None,
        "retained_attempt_identifier": trial.retained_attempt_id,
        "historical_failed_technical_attempt_count": trial.historical_failed_attempts,
        **complete_flags, "all_required_raw_artifacts_complete": all(complete_flags.values()),
    }


def _reconciliation_values(result: TrialResult) -> dict[str, Any]:
    row = result.row
    return {
        "allocator_call_count": row["total_valid_allocator_call_count"],
        "allocation_epoch_count": row["total_epoch_count"],
        "arrival_induced_trigger_count": sum(row[f"parent_epoch_reason_{reason}_call_count"] >= 0 for reason in ()) if False else sum(
            epoch["normalized_epoch_reason"] in ARRIVAL_REASONS for epoch in result.epoch_rows
        ),
        "mandatory_trigger_count": sum(epoch["mandatory"] for epoch in result.epoch_rows),
        "mandatory_reallocation_trigger_count": sum(epoch["mandatory"] and not epoch["initial"] for epoch in result.epoch_rows),
        "timeout_trigger_count": sum(epoch["normalized_epoch_reason"] == "age_timeout" for epoch in result.epoch_rows),
        "batch_threshold_trigger_count": sum(epoch["normalized_epoch_reason"] == "batch_threshold" for epoch in result.epoch_rows),
        "piggybacked_admission_epoch_count": sum(epoch["piggyback"] for epoch in result.epoch_rows),
        "mission_elapsed_time_s": _number(row.get("mission_elapsed_time_s")),
        "mean_release_to_first_assignment_latency_s": _stats(task["release_to_first_any_assignment_latency_s"] for task in result.tasks)["mean"],
        "median_release_to_first_assignment_latency_s": _stats(task["release_to_first_any_assignment_latency_s"] for task in result.tasks)["median"],
        "max_release_to_first_assignment_latency_s": _stats(task["release_to_first_any_assignment_latency_s"] for task in result.tasks)["max"],
        "mean_release_to_completion_latency_s": _stats(task["release_to_completion_latency_s"] for task in result.tasks)["mean"],
        "median_release_to_completion_latency_s": _stats(task["release_to_completion_latency_s"] for task in result.tasks)["median"],
        "max_release_to_completion_latency_s": _stats(task["release_to_completion_latency_s"] for task in result.tasks)["max"],
        "max_robot_steps": row["reconstructed_max_robot_steps"], "total_team_steps": row["reconstructed_total_team_steps"],
        "agx_allocator_processor_work_s": row["total_agx_allocator_work_s"],
        "rp2040_allocator_processor_work_s": row["total_rp2040_allocator_work_s"],
        "cumulative_allocator_time_s": row["injected_provider_work_s"],
        "all_tasks_completed": _truth(row.get("all_tasks_completed")),
        "algorithmic_failure_type": row.get("algorithmic_failure_type"),
        "task_events_sha256": row.get("task_events_sha256"),
        "allocation_epochs_sha256": row.get("allocation_epochs_sha256"),
    }


def _process_trial(
    trial: RawTrial,
    repository_root: Path,
    raw_root: Path,
    manifest_index: Mapping[str, Path],
    task_writer: GzipCsvWriter | None,
    epoch_writer: GzipCsvWriter,
    hardware_writer: GzipCsvWriter,
    include_full_call_writer: GzipCsvWriter | None,
    retain_semantic: bool,
    hardware_complexity: MutableMapping[tuple[Any, ...], dict[str, list[float]]],
) -> tuple[TrialResult, dict[str, Any], list[dict[str, Any]], list[dict[str, Any]]]:
    scenario_path, scenario, release_path, release_manifest = _resolve_trial_manifests(
        trial, repository_root, raw_root, manifest_index
    )
    task_path = trial.artifact_dir / "task_events.csv"
    call_path = trial.artifact_dir / "allocator_calls.csv"
    epoch_filename = "reallocation_events.csv" if (trial.artifact_dir / "reallocation_events.csv").is_file() else "allocation_epochs.csv"
    epoch_path = trial.artifact_dir / epoch_filename
    movement_path = trial.artifact_dir / "movement_events.csv"
    queue_path = trial.artifact_dir / "pending_queue_samples.csv"
    raw_tasks, task_meta = _read_csv_hashed(task_path)
    raw_epochs, epoch_meta = _read_csv_hashed(epoch_path)
    raw_calls, call_meta = _read_csv_hashed(call_path)
    raw_movements, movement_meta = _read_csv_hashed(movement_path)
    raw_queue, _ = _read_csv_hashed(queue_path)
    epochs = _normalize_epochs(trial, raw_epochs)
    calls = _normalize_calls(trial, raw_calls, epochs)
    all_movements = _normalize_movements(trial, raw_movements)
    tasks = _normalize_tasks(trial, raw_tasks, scenario, release_manifest, epochs, calls, raw_queue)
    horizon = _number(trial.publication.get("mission_elapsed_time_s")) or _number(trial.publication.get("algorithmic_horizon_time_s")) or _number(trial.summary.get("simulated_execution_time_s"))
    # The raw movement table is an append-only schedule and can retain one or
    # more in-flight moves whose completion lies after the mission stopped.
    # Robot steps, executed trajectories, final positions, and semantic event
    # sequences include only moves completed by the mission/horizon timestamp.
    movements = [
        movement for movement in all_movements
        if movement["completion_s"] is not None
        and (horizon is None or movement["completion_s"] <= horizon + TOLERANCE_S)
    ]
    semantic = _semantic_events(trial, tasks, epochs, calls, movements, horizon)
    summary_row = _trial_summary_row(trial, tasks, epochs, calls, movements, semantic, release_manifest, scenario)
    epoch_rows = _epoch_detail_rows(trial, epochs, calls, tasks, movements)
    call_summary = _call_summary_rows(trial, calls, tasks)
    admission_summary = _admission_summary_rows(trial, tasks)
    if task_writer is not None:
        for task in tasks:
            task_writer.writerow(_task_detail_row(trial, task))
    for row in epoch_rows:
        epoch_writer.writerow(row)
    epoch_map = {str(epoch["epoch_id"]): epoch for epoch in epochs}
    if trial.provider == "rp2040_hardware":
        for call in calls:
            hardware_row = _hardware_call_row(trial, call, epoch_map)
            hardware_writer.writerow(hardware_row)
            key = (
                trial.publication["algorithm"], trial.publication["arrival_load"], trial.publication["policy_id"],
                call["call_class"], call["parent_reason"], _bin_count(call["active_count"]),
                _bin_count(call["candidate_count"]), _bin_batch(epoch_map.get(call["epoch_id"], {}).get("admitted_count")),
                trial.publication.get("board_id") or trial.summary.get("board_id"),
            )
            bucket = hardware_complexity.setdefault(key, defaultdict(list))
            bucket["agx"].append(call["agx_total_s"])
            rp = call["rp_choose_s"] + call["rp_reset_s"]
            bucket["rp"].append(rp)
            if call["agx_total_s"] > 0:
                bucket["ratio"].append(rp / call["agx_total_s"])
    if include_full_call_writer is not None:
        for call in calls:
            include_full_call_writer.writerow(_hardware_call_row(trial, call, epoch_map))
    movement_signature = _hash_sequence((movement["robot_id"], movement["source"], movement["target"]) for movement in movements)
    assignment_signature = _hash_sequence((task["task_id"], task["first_assigned_robot"], task["first_assignment_source"]) for task in tasks if task["first_any_assignment_time_s"] is not None)
    completion_signature = _hash_sequence((task["task_id"], task["completion_robot"]) for task in sorted(tasks, key=lambda item: (item["completion_time_s"] if item["completion_time_s"] is not None else math.inf, item["task_id"])) if task["completion_time_s"] is not None)
    result = TrialResult(
        row=summary_row, tasks=tasks, epoch_rows=epoch_rows, calls=calls,
        movement_signature_sha256=movement_signature, assignment_signature_sha256=assignment_signature,
        completion_signature_sha256=completion_signature, semantic_events=semantic if retain_semantic else [],
    )
    metas = {"summary": trial.summary_meta, "tasks": task_meta, "calls": call_meta, "epochs": epoch_meta, "movements": movement_meta}
    raw_index = _raw_index_row(trial, scenario_path, release_path, metas, epoch_filename)
    return result, raw_index, call_summary, admission_summary


PAIR_METRICS = [
    "all_release_to_admission_mean_s",
    "online_release_to_admission_mean_s",
    "online_admission_to_first_any_assignment_mean_s",
    "online_admission_to_first_allocator_assignment_mean_s",
    "online_first_any_assignment_to_completion_mean_s",
    "online_release_to_first_any_assignment_mean_s",
    "online_release_to_first_allocator_assignment_mean_s",
    "online_release_to_completion_mean_s",
    "online_release_to_completion_median_s",
    "online_release_to_completion_q90_s",
    "online_release_to_completion_max_s",
    "all_release_to_completion_mean_s",
    "total_valid_allocator_call_count",
    "total_epoch_count",
    "calls_per_epoch",
    "observed_first_round_call_count",
    "post_first_round_call_count",
    "post_closure_call_count",
    "provider_allocator_processor_work_s",
    "injected_provider_work_s",
    "total_agx_allocator_work_s",
    "total_rp2040_allocator_work_s",
    "virtual_compute_union_diagnostic_s",
    "summed_per_robot_compute_union_diagnostic_s",
    "total_task_reassignment_count",
    "assignment_churn_per_completed_task",
    "reconstructed_max_robot_steps",
    "reconstructed_total_team_steps",
    "mission_elapsed_time_s",
] + [f"call_class_{value}_count" for value in CALL_CLASSES]


def _pair_delta(reference: Any, comparison: Any, both_complete: bool = True) -> tuple[Any, Any]:
    ref = _number(reference)
    comp = _number(comparison)
    if ref is None or comp is None or not both_complete:
        return None, None
    delta = comp - ref
    percent = (delta / ref * 100.0) if abs(ref) > 0 else None
    return delta, percent


def _provider_match_type(condition: str, reference: str) -> str:
    if condition == reference == "agx_host_proxy":
        return "same_agx"
    if condition == reference == "rp2040_hardware":
        return "same_rp2040"
    if condition == reference == "zero_compute":
        return "same_zero_compute"
    return "cross_provider"


def _policy_eager_pair(
    policy_row: Mapping[str, Any],
    eager_row: Mapping[str, Any],
    existing: Mapping[str, str] | None = None,
) -> dict[str, Any]:
    condition_provider = str(policy_row["provider"])
    reference_provider = str(eager_row["provider"])
    match = _provider_match_type(condition_provider, reference_provider)
    both_complete = _truth(policy_row.get("all_tasks_completed")) is True and _truth(eager_row.get("all_tasks_completed")) is True
    row: dict[str, Any] = dict(existing or {})
    row.update({
        "dataset": policy_row["dataset"], "algorithm": policy_row["algorithm"],
        "arrival_load": policy_row["arrival_load"], "policy_id": policy_row["policy_id"],
        "trace_id": policy_row["trace_id"], "trace_number": policy_row["trace_number"],
        "condition_provider": condition_provider, "reference_provider": reference_provider,
        "provider_pair": f"{condition_provider}__vs__{reference_provider}",
        "provider_match_type": match, "usable_for_primary_policy_effect": match != "cross_provider",
        "explicit_exclusion_reason": None if match != "cross_provider" else "condition and Eager reference use different timing providers",
        "policy_completed": _truth(policy_row.get("all_tasks_completed")),
        "eager_completed": _truth(eager_row.get("all_tasks_completed")),
        "both_completed": both_complete,
        "completion_discordance": _truth(policy_row.get("all_tasks_completed")) != _truth(eager_row.get("all_tasks_completed")),
    })
    for metric in PAIR_METRICS:
        require_complete = metric == "mission_elapsed_time_s"
        delta, percent = _pair_delta(eager_row.get(metric), policy_row.get(metric), both_complete or not require_complete)
        row[f"policy_{metric}"] = policy_row.get(metric)
        row[f"eager_{metric}"] = eager_row.get(metric)
        row[f"delta_{metric}"] = delta
        row[f"percent_change_{metric}"] = percent
    row["allocator_calls_saved"] = (
        _number(eager_row.get("total_valid_allocator_call_count")) - _number(policy_row.get("total_valid_allocator_call_count"))
        if _number(eager_row.get("total_valid_allocator_call_count")) is not None and _number(policy_row.get("total_valid_allocator_call_count")) is not None else None
    )
    row["allocator_processor_work_saved_s"] = (
        _number(eager_row.get("provider_allocator_processor_work_s")) - _number(policy_row.get("provider_allocator_processor_work_s"))
        if _number(eager_row.get("provider_allocator_processor_work_s")) is not None and _number(policy_row.get("provider_allocator_processor_work_s")) is not None else None
    )
    return row


def _policy_pairwise(reference: Mapping[str, Any], comparison: Mapping[str, Any]) -> dict[str, Any]:
    both_complete = _truth(reference.get("all_tasks_completed")) is True and _truth(comparison.get("all_tasks_completed")) is True
    row: dict[str, Any] = {
        "dataset": reference["dataset"], "algorithm": reference["algorithm"],
        "arrival_load": reference["arrival_load"], "trace_id": reference["trace_id"],
        "trace_number": reference["trace_number"], "reference_policy_id": reference["policy_id"],
        "comparison_policy_id": comparison["policy_id"], "reference_provider": reference["provider"],
        "comparison_provider": comparison["provider"], "provider_match_type": _provider_match_type(str(comparison["provider"]), str(reference["provider"])),
        "both_completed": both_complete,
        "completion_discordance": _truth(reference.get("all_tasks_completed")) != _truth(comparison.get("all_tasks_completed")),
    }
    for metric in PAIR_METRICS:
        require_complete = metric == "mission_elapsed_time_s"
        delta, percent = _pair_delta(reference.get(metric), comparison.get(metric), both_complete or not require_complete)
        row[f"reference_{metric}"] = reference.get(metric)
        row[f"comparison_{metric}"] = comparison.get(metric)
        row[f"delta_{metric}"] = delta
        row[f"percent_change_{metric}"] = percent
    row["allocator_calls_saved"] = (
        _number(reference.get("total_valid_allocator_call_count")) - _number(comparison.get("total_valid_allocator_call_count"))
        if _number(reference.get("total_valid_allocator_call_count")) is not None and _number(comparison.get("total_valid_allocator_call_count")) is not None else None
    )
    row["allocator_processor_work_saved_s"] = (
        _number(reference.get("provider_allocator_processor_work_s")) - _number(comparison.get("provider_allocator_processor_work_s"))
        if _number(reference.get("provider_allocator_processor_work_s")) is not None and _number(comparison.get("provider_allocator_processor_work_s")) is not None else None
    )
    return row


TASK_PAIR_FIELDS = [
    "dataset", "algorithm", "arrival_load", "policy_id", "trace_id", "trace_number",
    "policy_provider", "eager_provider", "trial_pair_key", "task_id", "is_initial_task", "is_online_task",
    "release_time_policy_s", "release_time_eager_s", "release_time_consistent",
    "admission_reason_policy", "admission_reason_eager",
    "release_to_admission_policy_s", "release_to_admission_eager_s", "delta_release_to_admission_s",
    "admission_to_first_any_assignment_policy_s", "admission_to_first_any_assignment_eager_s",
    "delta_admission_to_first_any_assignment_s", "release_to_first_any_assignment_policy_s",
    "release_to_first_any_assignment_eager_s", "delta_release_to_first_any_assignment_s",
    "release_to_first_allocator_assignment_policy_s", "release_to_first_allocator_assignment_eager_s",
    "delta_release_to_first_allocator_assignment_s", "assignment_source_policy", "assignment_source_eager",
    "first_assigned_robot_policy", "first_assigned_robot_eager", "owner_change_count_policy",
    "owner_change_count_eager", "release_to_completion_policy_s", "release_to_completion_eager_s",
    "delta_release_to_completion_s", "completion_robot_policy", "completion_robot_eager",
    "mission_ending_task_policy", "mission_ending_task_eager", "trace_id_clustering_unit",
]


def _task_pair_rows(policy: TrialResult, eager: TrialResult) -> Iterator[dict[str, Any]]:
    p_by_id = {task["task_id"]: task for task in policy.tasks}
    e_by_id = {task["task_id"]: task for task in eager.tasks}
    for task_id in sorted(p_by_id):
        p = p_by_id[task_id]
        e = e_by_id[task_id]
        row = {
            "dataset": policy.row["dataset"], "algorithm": policy.row["algorithm"],
            "arrival_load": policy.row["arrival_load"], "policy_id": policy.row["policy_id"],
            "trace_id": policy.row["trace_id"], "trace_number": policy.row["trace_number"],
            "policy_provider": policy.row["provider"], "eager_provider": eager.row["provider"],
            "trial_pair_key": f"{policy.row['dataset']}__{policy.row['algorithm']}__{policy.row['arrival_load']}__{policy.row['trace_id']}__{policy.row['policy_id']}__vs__eager_b1",
            "task_id": task_id, "is_initial_task": p["is_initial_task"], "is_online_task": p["is_online_task"],
            "release_time_policy_s": p["release_time_s"], "release_time_eager_s": e["release_time_s"],
            "release_time_consistent": abs(p["release_time_s"] - e["release_time_s"]) <= TOLERANCE_S,
            "admission_reason_policy": p["normalized_admission_reason"], "admission_reason_eager": e["normalized_admission_reason"],
            "assignment_source_policy": p["first_assignment_source"], "assignment_source_eager": e["first_assignment_source"],
            "first_assigned_robot_policy": p["first_assigned_robot"], "first_assigned_robot_eager": e["first_assigned_robot"],
            "owner_change_count_policy": p["distinct_owner_change_count"], "owner_change_count_eager": e["distinct_owner_change_count"],
            "completion_robot_policy": p["completion_robot"], "completion_robot_eager": e["completion_robot"],
            "mission_ending_task_policy": p["mission_ending_task"], "mission_ending_task_eager": e["mission_ending_task"],
            "trace_id_clustering_unit": policy.row["trace_id"],
        }
        families = {
            "release_to_admission": "release_to_admission_latency_s",
            "admission_to_first_any_assignment": "admission_to_first_any_assignment_latency_s",
            "release_to_first_any_assignment": "release_to_first_any_assignment_latency_s",
            "release_to_first_allocator_assignment": "release_to_first_allocator_assignment_latency_s",
            "release_to_completion": "release_to_completion_latency_s",
        }
        for prefix, field_name in families.items():
            pv, ev = p[field_name], e[field_name]
            row[f"{prefix}_policy_s"] = pv
            row[f"{prefix}_eager_s"] = ev
            row[f"delta_{prefix}_s"] = pv - ev if pv is not None and ev is not None else None
        yield row


CAUSAL_ZERO_METRICS = [
    "online_release_to_admission_mean_s", "online_admission_to_first_any_assignment_mean_s",
    "online_admission_to_first_allocator_assignment_mean_s", "online_release_to_first_any_assignment_mean_s",
    "online_release_to_completion_mean_s", "online_release_to_completion_median_s",
    "total_valid_allocator_call_count", "total_epoch_count", "calls_per_epoch",
    "provider_allocator_processor_work_s", "total_agx_allocator_work_s", "total_rp2040_allocator_work_s",
    "virtual_compute_union_diagnostic_s", "summed_per_robot_compute_union_diagnostic_s",
    "total_task_reassignment_count", "robot_goal_change_count", "reconstructed_max_robot_steps",
    "reconstructed_total_team_steps", "mission_elapsed_time_s",
] + [f"call_class_{value}_count" for value in CALL_CLASSES]


def _causal_zero_pair(causal: Mapping[str, Any], zero: Mapping[str, Any]) -> dict[str, Any]:
    c = causal["row"]
    z = zero["row"]
    c_complete = _truth(c.get("all_tasks_completed")) is True
    z_complete = _truth(z.get("all_tasks_completed")) is True
    row: dict[str, Any] = {
        "algorithm": c["algorithm"], "arrival_load": c["arrival_load"], "policy_id": c["policy_id"],
        "trace_id": c["trace_id"], "trace_number": c["trace_number"], "causal_provider": c["provider"],
        "zero_provider": z["provider"], "runtime_seed_match": str(c.get("runtime_seed")) == str(z.get("runtime_seed")),
        "scenario_sha256_match": c.get("scenario_sha256") == z.get("scenario_sha256"),
        "release_sha256_match": c.get("release_sha256") == z.get("release_sha256"),
        "both_complete": c_complete and z_complete, "causal_only_noncompletion": not c_complete and z_complete,
        "zero_only_noncompletion": c_complete and not z_complete,
        "path_assignment_identity": causal["assignment_signature_sha256"] == zero["assignment_signature_sha256"],
        "identical_task_owner_sequence": causal["assignment_signature_sha256"] == zero["assignment_signature_sha256"],
        "identical_movement_edge_sequence": causal["movement_signature_sha256"] == zero["movement_signature_sha256"],
        "identical_completion_sequence": causal["completion_signature_sha256"] == zero["completion_signature_sha256"],
    }
    if row["identical_task_owner_sequence"] is False:
        row["first_semantic_divergence_category"] = "first_assignment_owner_or_source"
    elif row["identical_movement_edge_sequence"] is False:
        row["first_semantic_divergence_category"] = "movement_edge"
    elif row["identical_completion_sequence"] is False:
        row["first_semantic_divergence_category"] = "task_completion_order_or_robot"
    else:
        row["first_semantic_divergence_category"] = "none_reconstructible"
    for metric in CAUSAL_ZERO_METRICS:
        cv, zv = c.get(metric), z.get(metric)
        require_complete = metric == "mission_elapsed_time_s"
        row[f"causal_{metric}"] = cv
        row[f"zero_{metric}"] = zv
        row[f"causal_minus_zero_{metric}"] = (
            _number(cv) - _number(zv)
            if _number(cv) is not None and _number(zv) is not None and (not require_complete or c_complete and z_complete)
            else None
        )
    return row


CBAA_LIVENESS_FIELDS = [
    *IDENTIFIER_FIELDS, "completion_outcome", "released_count", "admitted_count", "assigned_count",
    "completed_count", "algorithmic_horizon_type", "horizon_time_s", "last_progress_time_s",
    "next_release_time_s", "total_calls", "final_window_calls", "total_epochs", "final_window_epochs",
    "call_classes_final_500_events", "call_classes_final_1000_events", "epoch_reasons_final_500_events",
    "epoch_reasons_final_1000_events", "repeated_state_hash_frequency", "repeated_goal_robot_call_signature_frequency",
    "assignment_churn", "robot_idle_count", "invalid_goal_count", "pending_depth", "active_task_count",
    "final_positions", "exact_causal_zero_pair_completed", "eager_failed_same_trace_coalesced_completed",
    "eager_completed_same_trace_coalesced_failed",
]


def _cbaa_liveness_rows(summary_results: Mapping[tuple[str, str, str, str, str], Mapping[str, Any]]) -> list[dict[str, Any]]:
    rows = []
    for key, compact in sorted(summary_results.items()):
        row = compact["row"]
        if row["algorithm"] != "CBAA" or row["dataset"] not in {"causal", "zero_compute"}:
            continue
        opposite_dataset = "zero_compute" if row["dataset"] == "causal" else "causal"
        pair = summary_results.get((opposite_dataset, "CBAA", row["arrival_load"], row["policy_id"], row["trace_id"]))
        same_trace = [
            item["row"] for candidate, item in summary_results.items()
            if candidate[0] == row["dataset"] and candidate[1] == "CBAA" and candidate[2] == row["arrival_load"]
            and candidate[4] == row["trace_id"] and candidate[3] != "eager_b1"
        ]
        eager = summary_results.get((row["dataset"], "CBAA", row["arrival_load"], "eager_b1", row["trace_id"]))
        rows.append({
            **{field: row.get(field) for field in IDENTIFIER_FIELDS},
            "completion_outcome": "completed" if _truth(row.get("all_tasks_completed")) is True else "algorithmic_noncompletion",
            "released_count": row["released_task_count_at_end"], "admitted_count": row["admitted_task_count_at_end"],
            "assigned_count": row["assigned_task_count_at_end"], "completed_count": row["completed_task_count_at_end"],
            "algorithmic_horizon_type": row["algorithmic_horizon_type"], "horizon_time_s": row["horizon_time_s"],
            "last_progress_time_s": row["last_progress_timestamp_s"], "next_release_time_s": row["next_scheduled_release_after_stop_s"],
            "total_calls": row["total_valid_allocator_call_count"], "final_window_calls": row["calls_since_last_progress"],
            "total_epochs": row["total_epoch_count"], "final_window_epochs": row["epochs_since_last_progress"],
            "call_classes_final_500_events": row["final_500_semantic_event_call_classes"],
            "call_classes_final_1000_events": row["final_1000_semantic_event_call_classes"],
            "epoch_reasons_final_500_events": row["final_500_semantic_event_epoch_reasons"],
            "epoch_reasons_final_1000_events": row["final_1000_semantic_event_epoch_reasons"],
            "repeated_state_hash_frequency": row["repeated_final_state_signature_count"],
            "repeated_goal_robot_call_signature_frequency": None,
            "assignment_churn": row["total_task_reassignment_count"], "robot_idle_count": row["robot_idle_event_count"],
            "invalid_goal_count": row["invalid_goal_event_count"], "pending_depth": row["final_pending_task_count"],
            "active_task_count": row["final_active_task_count"], "final_positions": row["final_robot_positions"],
            "exact_causal_zero_pair_completed": _truth(pair["row"].get("all_tasks_completed")) if pair else None,
            "eager_failed_same_trace_coalesced_completed": (
                eager is not None and _truth(eager["row"].get("all_tasks_completed")) is False
                and any(_truth(value.get("all_tasks_completed")) is True for value in same_trace)
            ),
            "eager_completed_same_trace_coalesced_failed": (
                eager is not None and _truth(eager["row"].get("all_tasks_completed")) is True
                and any(_truth(value.get("all_tasks_completed")) is False for value in same_trace)
            ),
        })
    return rows


def _category_sequence(events: Sequence[Mapping[str, Any]], category: str) -> list[tuple[Any, ...]]:
    if category == "allocator_goal":
        return [(event.get("robot_id"), event.get("goal")) for event in events if event.get("source") == "call" and event.get("goal") is not None]
    if category == "task_assignment":
        return [(event.get("task_id"), event.get("owner"), event.get("reason")) for event in events if event.get("kind") == "first_assignment"]
    if category == "movement_edge":
        return [(event.get("robot_id"), event.get("reason")) for event in events if event.get("kind") == "movement_edge"]
    if category == "task_completion":
        return [(event.get("task_id"), event.get("robot_id")) for event in events if event.get("kind") == "completion"]
    return []


def _first_difference(left: Sequence[Any], right: Sequence[Any]) -> tuple[int | None, Any, Any]:
    for index, (a, b) in enumerate(itertools.zip_longest(left, right, fillvalue=None)):
        if a != b:
            return index, a, b
    return None, None, None


DIVERGENCE_FIELDS = [
    "comparison_id", "comparison_kind", "algorithm", "arrival_load", "trace_id", "failed_policy_id",
    "reference_dataset", "reference_policy_id", "comparison_dataset", "comparison_policy_id",
    "first_differing_allocator_goal", "first_differing_task_assignment", "first_differing_owner_sequence",
    "first_differing_movement_edge", "first_differing_task_completion", "first_semantic_divergence_category",
    "first_semantic_divergence_reference_index", "first_semantic_divergence_comparison_index",
    "first_repeating_no_progress_signature_time_s", "events_between_divergence_and_horizon",
    "calls_between_divergence_and_horizon", "epochs_between_divergence_and_horizon",
    "meaningful_semantic_divergence_reconstructible", "limitations",
]


def _divergence_compare(
    comparison_id: str,
    kind: str,
    reference: Mapping[str, Any],
    comparison: Mapping[str, Any],
) -> dict[str, Any]:
    ref_events = reference.get("semantic_events", [])
    cmp_events = comparison.get("semantic_events", [])
    differences: dict[str, tuple[int | None, Any, Any]] = {}
    for category in ("allocator_goal", "task_assignment", "movement_edge", "task_completion"):
        differences[category] = _first_difference(_category_sequence(ref_events, category), _category_sequence(cmp_events, category))
    candidates = [(index, category) for category, (index, _, _) in differences.items() if index is not None]
    category = min(candidates)[1] if candidates else None
    cmp_index = differences[category][0] if category else None
    semantic_filtered = [event for event in cmp_events if event.get("kind") in {"allocator_call", "first_assignment", "movement_edge", "completion"}]
    divergence_event = semantic_filtered[cmp_index] if cmp_index is not None and cmp_index < len(semantic_filtered) else None
    divergence_time = divergence_event.get("time_s") if divergence_event else None
    horizon = _number(comparison["row"].get("horizon_time_s")) or _number(comparison["row"].get("mission_elapsed_time_s"))
    after = [event for event in cmp_events if divergence_time is not None and event.get("time_s") is not None and event["time_s"] >= divergence_time - TOLERANCE_S]
    state_events = [event for event in cmp_events if event.get("source") == "call" and event.get("state_hash")]
    repeated = Counter(event["state_hash"] for event in state_events[-1000:])
    dominant_hash = _dominant(repeated)
    repeating_time = next((event["time_s"] for event in state_events if event["state_hash"] == dominant_hash), None) if dominant_hash and repeated[dominant_hash] > 1 else None
    return {
        "comparison_id": comparison_id, "comparison_kind": kind, "algorithm": comparison["row"]["algorithm"],
        "arrival_load": comparison["row"]["arrival_load"], "trace_id": comparison["row"]["trace_id"],
        "failed_policy_id": comparison["row"]["policy_id"], "reference_dataset": reference["row"]["dataset"],
        "reference_policy_id": reference["row"]["policy_id"], "comparison_dataset": comparison["row"]["dataset"],
        "comparison_policy_id": comparison["row"]["policy_id"],
        "first_differing_allocator_goal": differences["allocator_goal"],
        "first_differing_task_assignment": differences["task_assignment"],
        "first_differing_owner_sequence": differences["task_assignment"],
        "first_differing_movement_edge": differences["movement_edge"],
        "first_differing_task_completion": differences["task_completion"],
        "first_semantic_divergence_category": category,
        "first_semantic_divergence_reference_index": differences[category][0] if category else None,
        "first_semantic_divergence_comparison_index": cmp_index,
        "first_repeating_no_progress_signature_time_s": repeating_time,
        "events_between_divergence_and_horizon": len(after) if divergence_time is not None and horizon is not None else None,
        "calls_between_divergence_and_horizon": sum(event.get("source") == "call" for event in after) if after else None,
        "epochs_between_divergence_and_horizon": sum(event.get("source") == "epoch" for event in after) if after else None,
        "meaningful_semantic_divergence_reconstructible": category is not None,
        "limitations": "raw unified event/message log and repeated owner history were not retained; comparison uses normalized goal/first-assignment/movement/completion sequences and ignores duration-only timestamp shifts",
        "_reference_events": ref_events, "_comparison_events": cmp_events,
    }


def _matrix_coverage_rows(rows: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    groups: dict[tuple[str, str, str, str, str], list[Mapping[str, Any]]] = defaultdict(list)
    all_by_key = {_trial_key(row): row for row in rows}
    for row in rows:
        groups[(str(row["dataset"]), str(row["provider"]), str(row["algorithm"]), str(row["arrival_load"]), str(row["policy_id"]))].append(row)
    result = []
    for key, values in sorted(groups.items(), key=lambda item: _sort_key(item[1][0])):
        dataset, provider, algorithm, load, policy = key
        eager_available = sum(
            (dataset, algorithm, load, "eager_b1", value["trace_id"]) in all_by_key
            and all_by_key[(dataset, algorithm, load, "eager_b1", value["trace_id"])]["provider"] == provider
            for value in values
        )
        result.append({
            "dataset": dataset, "provider": provider, "algorithm": algorithm, "arrival_load": load,
            "policy_id": policy, "planned_trial_count": len(values), "retained_trial_count": len(values),
            "completed_count": sum(_truth(value.get("all_tasks_completed")) is True for value in values),
            "algorithmic_noncompletion_count": sum(_truth(value.get("all_tasks_completed")) is False for value in values),
            "unique_trace_count": len({value["trace_id"] for value in values}),
            "unique_board_count": len({value.get("board_id") for value in values if value.get("board_id")}),
            "raw_call_file_coverage": sum(_truth(value.get("raw_call_file_complete")) is True for value in values),
            "raw_epoch_file_coverage": sum(_truth(value.get("raw_epoch_file_complete")) is True for value in values),
            "raw_task_file_coverage": sum(_truth(value.get("raw_task_file_complete")) is True for value in values),
            "raw_movement_file_coverage": sum(_truth(value.get("raw_movement_file_complete")) is True for value in values),
            "same_provider_eager_reference_available_count": eager_available,
            "same_provider_eager_reference_available_for_all": eager_available == len(values),
            "balanced_96_trial_hardware_core_cell": provider == "rp2040_hardware" and policy in {"eager_b1", "count_b4"},
            "eight_case_bounded_hardware_cell": provider == "rp2040_hardware" and policy == "bounded_b4_w5",
            "limited_replication_warning": (
                "hardware rows use four unique core traces, not independent scenario replication"
                if provider == "rp2040_hardware" and policy in {"eager_b1", "count_b4"}
                else "bounded hardware cell has one trace" if provider == "rp2040_hardware" else None
            ),
        })
    return result


HARDWARE_COMPLEXITY_FIELDS = [
    "algorithm", "arrival_load", "policy_id", "call_class", "epoch_reason", "active_task_count_bin",
    "candidate_count_bin", "admitted_batch_size_bin", "board_id", "call_count",
] + [f"{prefix}_{stat}" for prefix in ("agx_duration_s", "rp2040_duration_s", "rp2040_to_agx_ratio") for stat in ("mean", "median", "iqr", "q90", "q95", "max")]


def _hardware_complexity_rows(values: Mapping[tuple[Any, ...], Mapping[str, Sequence[float]]]) -> list[dict[str, Any]]:
    rows = []
    for key, bucket in sorted(values.items()):
        row = dict(zip(HARDWARE_COMPLEXITY_FIELDS[:9], key))
        row["call_count"] = len(bucket["rp"])
        for prefix, name in (("agx_duration_s", "agx"), ("rp2040_duration_s", "rp"), ("rp2040_to_agx_ratio", "ratio")):
            summary = _stats(bucket[name])
            row[f"{prefix}_mean"] = summary["mean"]
            row[f"{prefix}_median"] = summary["median"]
            row[f"{prefix}_iqr"] = summary["q3"] - summary["q1"] if summary["q3"] is not None else None
            for stat in ("q90", "q95", "max"):
                row[f"{prefix}_{stat}"] = summary[stat]
        rows.append(row)
    return rows


HARDWARE_ENV_FIELDS = [
    "environment_id", "row_type", "board_id", "board_uid", "worker_id", "board_model",
    "micropython_version", "cpu_clock_hz", "firmware_build_identifier", "firmware_sha256",
    "module_set_sha256", "timer_resolution_us", "serial_port", "baud_rate", "protocol_version",
    "worker_core_affinity", "logical_robot_contexts_per_board", "agx_power_mode", "jetson_clocks_state",
    "thermal_information", "campaign_start", "campaign_end", "source_evidence_path", "source_evidence_sha256",
    "missing_evidence_note",
]


def _hardware_environment_rows(repository_root: Path, campaigns: Mapping[str, Path], raw_index: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    bindings_path = repository_root / "configs/local/agx_board_bindings.json"
    bindings = json.loads(bindings_path.read_text(encoding="utf-8")) if bindings_path.is_file() else {"boards": []}
    timer_by_board = {item["board_id"]: item for item in bindings.get("timer_evidence_at_binding", [])}
    dates = []
    for label in ("hardware_core", "hardware_bounded"):
        root = campaigns.get(label)
        if root is None:
            continue
        for path in (root / "provenance").glob("*.json"):
            value = json.loads(path.read_text(encoding="utf-8"))
            if value.get("created_at"):
                dates.append(str(value["created_at"]))
    rows = []
    for index, board in enumerate(bindings.get("boards", [])):
        timer = timer_by_board.get(board["board_id"], {})
        rows.append({
            "environment_id": board["board_id"], "row_type": "rp2040_board", "board_id": board["board_id"],
            "board_uid": board.get("expected_device_uid"), "worker_id": index, "board_model": "Pololu 3pi+ 2040 (RP2040)",
            "micropython_version": timer.get("implementation"), "cpu_clock_hz": timer.get("frequency_hz"),
            "firmware_build_identifier": board.get("expected_build_id"), "firmware_sha256": board.get("expected_firmware_sha256"),
            "module_set_sha256": board.get("expected_module_set_sha256"), "timer_resolution_us": timer.get("timer_resolution_us"),
            "serial_port": board.get("serial_device"), "baud_rate": None, "protocol_version": "persistent allocator replay schema 1",
            "worker_core_affinity": index, "logical_robot_contexts_per_board": 4,
            "campaign_start": min(dates) if dates else None, "campaign_end": max(dates) if dates else None,
            "source_evidence_path": str(bindings_path.resolve()) if bindings_path.is_file() else None,
            "source_evidence_sha256": _sha256(bindings_path) if bindings_path.is_file() else None,
            "missing_evidence_note": "baud rate and physical host-wall timestamps were not retained in the promoted evidence",
        })
    architecture = repository_root / "study/output/agx_deadline_aug14_v1/fix_worktree/docs/SIMULATION_ARCHITECTURE.md"
    rows.append({
        "environment_id": "agx_host", "row_type": "agx_host", "board_model": "NVIDIA Jetson AGX Orin",
        "worker_core_affinity": "hardware workers CPUs 0-2; rolling AGX pool CPUs 3-8; CPUs 9-11 reserved",
        "logical_robot_contexts_per_board": None, "agx_power_mode": None, "jetson_clocks_state": None,
        "thermal_information": None, "campaign_start": min(dates) if dates else None, "campaign_end": max(dates) if dates else None,
        "source_evidence_path": str(architecture.resolve()) if architecture.is_file() else None,
        "source_evidence_sha256": _sha256(architecture) if architecture.is_file() else None,
        "missing_evidence_note": "AGX power mode, jetson_clocks state, thermal readings, and cross-worker host-wall call timestamps were not retained",
    })
    return rows


def _representative_specs() -> list[tuple[str, str, list[tuple[str, str, str, str, str]]]]:
    return [
        ("A", "HIPC hardware benefit", [("causal", "HIPC", "high", p, "trace_0002") for p in ("eager_b1", "count_b4")]),
        ("B", "ACBBA work saving but mission loss", [("causal", "ACBBA", "high", p, "trace_0000") for p in ("eager_b1", "count_b4")]),
        ("C", "low-load epoch/call reversal", [("zero_compute", "CBAA", "low", p, "trace_0000") for p in ("eager_b1", "count_b4")]),
        ("D", "bounded timeout behavior", [("timeout_verification", "ACBBA", "low", p, "trace_0000") for p in ("eager_b1", "count_b4", "bounded_b4_w5")]),
        ("E", "CBAA liveness interaction", [
            ("causal", "CBAA", "low", "eager_b1", "trace_0004"),
            ("zero_compute", "CBAA", "low", "eager_b1", "trace_0004"),
            ("causal", "CBAA", "low", "count_b4", "trace_0004"),
            ("causal", "CBAA", "low", "bounded_b4_w5", "trace_0004"),
        ]),
        ("F", "PI hardware service-latency case", [("causal", "PI", "high", p, "trace_0002") for p in ("eager_b1", "count_b4")]),
    ]


def _representative_rows(summary_results: Mapping[tuple[str, str, str, str, str], Mapping[str, Any]]) -> list[dict[str, Any]]:
    rows = []
    for case_id, label, keys in _representative_specs():
        for key in keys:
            compact = summary_results.get(key)
            if compact is None:
                continue
            trial = compact["row"]
            role = f"{trial['dataset']}:{trial['provider']}:{trial['policy_id']}"
            for event in compact.get("semantic_events", []):
                rows.append({
                    "case_id": case_id, "case_label": label, "comparison_role": role, "record_type": "event",
                    **{field: trial.get(field) for field in IDENTIFIER_FIELDS},
                    "sequence_index": event.get("sequence_index"), "event_time_s": event.get("time_s"),
                    "source_stream": event.get("source"), "event_kind": event.get("kind"),
                    "robot_id": event.get("robot_id"), "task_id": event.get("task_id"),
                    "epoch_id": event.get("epoch_id"), "call_id": event.get("call_id"),
                    "reason_or_class": event.get("reason"), "goal": event.get("goal"), "owner": event.get("owner"),
                    "state_hash": event.get("state_hash"), "details_sha256": event.get("details_sha256"),
                    "released_task_count": event.get("released_count"), "admitted_task_count": event.get("admitted_count"),
                    "completed_task_count": event.get("completed_count"), "progress_signature_sha256": event.get("progress_signature_sha256"),
                })
            for task in compact.get("tasks", []):
                if not task["is_online_task"]:
                    continue
                rows.append({
                    "case_id": case_id, "case_label": label, "comparison_role": role, "record_type": "online_task_latency",
                    **{field: trial.get(field) for field in IDENTIFIER_FIELDS}, "task_id": task["task_id"],
                    "release_time_s": task["release_time_s"], "admission_time_s": task["admission_time_s"],
                    "first_assignment_time_s": task["first_any_assignment_time_s"], "completion_time_s": task["completion_time_s"],
                    "release_to_admission_latency_s": task["release_to_admission_latency_s"],
                    "admission_to_first_assignment_latency_s": task["admission_to_first_any_assignment_latency_s"],
                    "first_assignment_to_completion_latency_s": task["first_any_assignment_to_completion_latency_s"],
                    "release_to_completion_latency_s": task["release_to_completion_latency_s"],
                })
    return sorted(rows, key=lambda row: (row["case_id"], row["comparison_role"], row["record_type"], _number(row.get("event_time_s")) or _number(row.get("release_time_s")) or -1, str(row.get("task_id", ""))))


DIVERGENCE_EVENT_FIELDS = [
    "comparison_id", "comparison_side", "window_offset", "source_stream", "sequence_index", "event_time_s",
    "event_kind", "robot_id", "task_id", "epoch_id", "call_id", "reason_or_class", "goal", "owner",
    "relevant_state_hash", "released_task_count", "admitted_task_count", "completed_task_count",
    "progress_signature_sha256", "meaningful_divergence_reconstructible",
]


def _divergence_event_rows(divergences: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    rows = []
    for divergence in divergences:
        reconstructible = bool(divergence["meaningful_semantic_divergence_reconstructible"])
        for side, events in (("reference", divergence.get("_reference_events", [])), ("comparison", divergence.get("_comparison_events", []))):
            if not events:
                rows.append({
                    "comparison_id": divergence["comparison_id"], "comparison_side": side,
                    "meaningful_divergence_reconstructible": False,
                })
                continue
            target = divergence.get("first_semantic_divergence_comparison_index") or 0
            target = max(0, min(int(target), len(events) - 1))
            left, right = max(0, target - 25), min(len(events), target + 51)
            for index in range(left, right):
                event = events[index]
                rows.append({
                    "comparison_id": divergence["comparison_id"], "comparison_side": side,
                    "window_offset": index - target, "source_stream": event.get("source"),
                    "sequence_index": event.get("sequence_index"), "event_time_s": event.get("time_s"),
                    "event_kind": event.get("kind"), "robot_id": event.get("robot_id"),
                    "task_id": event.get("task_id"), "epoch_id": event.get("epoch_id"), "call_id": event.get("call_id"),
                    "reason_or_class": event.get("reason"), "goal": event.get("goal"), "owner": event.get("owner"),
                    "relevant_state_hash": event.get("state_hash"), "released_task_count": event.get("released_count"),
                    "admitted_task_count": event.get("admitted_count"), "completed_task_count": event.get("completed_count"),
                    "progress_signature_sha256": event.get("progress_signature_sha256"),
                    "meaningful_divergence_reconstructible": reconstructible,
                })
    return rows


MISSING_ROWS = [
    ("task and trial assignment diagnostics", "repeated_same_owner_assignment_count; distinct_owner_change_count; unique_owner_count; final_assigned_owner", "all trials", "task_events retains first owner plus aggregate assignment/reassignment counts, not the owner history"),
    ("trajectory counters", "turn_count; replan_count; blocked_intent_collision_count; quarantine_count", "all trials", "robot counter internals were not included in promoted CSV/JSON artifacts"),
    ("allocator internals", "bundle_reset_count; task_goal_change_count", "all trials", "no explicit algorithm-neutral event was retained; values are not inferred from algorithm names"),
    ("message mechanism", "inbound_message_count; outbound_message_count; buffered_message_count", "all trials", "message payload hashes are retained per call but message event counts/timestamps are not"),
    ("compute buffering", "maximum_compute_buffering_delay_s", "all trials", "release/admission overlap with virtual compute is derivable, but a causal delay attribution is not recorded"),
    ("task release geometry", "minimum_robot_distance_at_release", "all trials", "robots may be in transit at release and continuous within-edge positions were not retained"),
    ("hardware physical concurrency", "physical host wall start/end; cross-worker overlap counts", "104 hardware trials", "physical measurement order is retained per board, but host-wall timestamps are not"),
    ("hardware protocol diagnostics", "protocol_retry_count; device_timeout; result_serialization_duration_s", "104 hardware trials", "these subfields were not retained separately in promoted call records"),
    ("hardware environment", "baud_rate; AGX power mode; jetson_clocks; thermals", "hardware environment", "no retained evidence artifact records these fields"),
    ("noncompletion event loop", "total_processed_event_count; stagnation_event_count", "algorithmic noncompletions", "the summary retains configured horizons and final semantic streams, not the scheduler's processed-event counter"),
    ("CBAA repeated signatures", "repeated_goal_robot_call_signature_frequency", "CBAA trials", "goal and state hashes are retained per call, but the full progress signature used internally is not"),
    ("architecture filename", "ARCHITECTURE_CLARIFICATION.md", "repository provenance", "the named file is absent; retained docs/SIMULATION_ARCHITECTURE.md is used and the discrepancy is documented"),
]


def _missing_audit_rows() -> list[dict[str, Any]]:
    return [
        {
            "missing_data_id": f"M{index:03d}", "output_area": area, "fields": fields,
            "scope": scope, "missing_value_representation": "empty field", "reason": reason,
            "imputation_performed": False, "scientific_consequence": "field is excluded from quantitative interpretation; related retained proxies remain explicitly labeled",
        }
        for index, (area, fields, scope, reason) in enumerate(MISSING_ROWS, 1)
    ]


def _validation_row(check_id: str, severity: str, expected: Any, observed: Any, passed: bool, details: str = "") -> dict[str, Any]:
    return {
        "check_id": check_id, "severity": severity, "expected_value": expected,
        "observed_value": observed, "pass": bool(passed), "details": details,
    }


def _source_line_reference(column: str) -> str:
    if "assignment" in column or "completion" in column or "release" in column or "admission" in column:
        return "known_visit_sim/core/world.py:120; known_visit_sim/core/world.py:142; known_visit_sim/core/world.py:190"
    if "epoch" in column or "pending" in column or "piggyback" in column or "timeout" in column:
        return "known_visit_sim/core/reallocation.py:390; known_visit_sim/core/reallocation.py:620"
    if "call" in column or "work" in column or "compute" in column:
        return "known_visit_sim/core/reallocation.py:620; known_visit_sim/core/timing.py:300"
    if "movement" in column or "step" in column:
        return "known_visit_sim/core/scheduler.py:200"
    return "scripts/agx_build_final_publication.py:918; build_paper_v2_data_bundle.py"


def _dictionary_metadata(file_name: str, column: str) -> dict[str, Any]:
    units = "s" if column.endswith("_s") or "time_s" in column or "latency_s" in column or "duration_s" in column else (
        "percent" if "percent" in column else "ratio" if "fraction" in column or "ratio" in column or "rate" in column else "count" if "count" in column or column.endswith("steps") else ""
    )
    if any(token in column for token in ("rp2040", "device_")) and ("duration" in column or "work" in column):
        clock = "RP2040 device timer"
    elif "physical_host" in column or "host_wall" in column:
        clock = "host program wall"
    elif "agx" in column and ("duration" in column or "work" in column):
        clock = "AGX monotonic wall"
    elif units == "count" or "fraction" in column or "ratio" in column:
        clock = "derived count"
    else:
        clock = "simulated/event"
    formula = "copied verbatim from the named raw/publication field"
    if column.startswith("delta_") or "causal_minus_zero" in column:
        formula = "comparison value minus reference value; mission-time delta only when both trials completed"
    elif "release_to_completion" in column:
        formula = "completion/search timestamp minus predetermined release timestamp; online summaries exclude eight time-zero tasks"
    elif "release_to_admission" in column:
        formula = "admission timestamp minus release timestamp"
    elif "admission_to_first" in column:
        formula = "first-assignment timestamp minus admission timestamp; allocator-only variants exclude physical-service fallback"
    elif "first_any_assignment_to_completion" in column or "assignment_to_completion" in column:
        formula = "physical completion/search timestamp minus any-source first-assignment timestamp"
    elif "processor_work" in column or "work_sum" in column:
        formula = "sum of per-call processor durations across calls and logical robots; not mission wall delay"
    elif "compute_union" in column:
        formula = "measure of the union of retained virtual call [start, completion] intervals"
    elif "first_round" in column:
        formula = "first valid associated call by each expected robot in the epoch; hardware expected set inferred as documented"
    elif "post_closure" in column:
        formula = "associated valid call completion exceeds epoch first-round close time by more than 1e-9 s"
    elif "provider_match" in column or "provider_pair" in column:
        formula = "classification of condition and reference timing providers; cross-provider rows are audit-only"
    elif "step" in column or "movement" in column or "final_positions" in column:
        formula = "count/sequence of movement edges completed no later than the mission or algorithmic-horizon stop timestamp; post-stop scheduled edges are excluded"
    missing = "empty means unavailable/not applicable; zero is emitted only for an observed zero"
    return {
        "file_name": file_name, "column_name": column, "data_type": "mixed/string" if column.endswith("_id") or "reason" in column or "hash" in column else "number/boolean/string",
        "units": units, "clock_source": clock, "precise_formula": formula,
        "accumulation_scope": "scope encoded by file keys and column prefix",
        "inclusion_exclusion_rules": "valid/parity-accepted calls only for scientific call/work aggregates; trace remains replication unit",
        "missing_value_interpretation": missing, "source_raw_artifact": "task_events.csv; allocation_epochs.csv/reallocation_events.csv; allocator_calls.csv; movement_events.csv; manifests as applicable",
        "source_repository_file_and_line_reference": _source_line_reference(column),
        "notes": "Type-7 linearly interpolated descriptive quantiles; no task/call/epoch inferential statistics",
    }


def _write_text(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content.rstrip() + "\n", encoding="utf-8")


def _readme_text(command: str) -> str:
    return f"""# Paper-V2 latency and mechanism data bundle

This is a read-only descriptive export from the immutable August 2026 MRTA
campaigns.  No simulation or hardware experiment was rerun.  The primary
mission-performance outcome is online-task release-to-completion latency: time
from an online task becoming available until it is physically completed or
searched.  Mission elapsed time is secondary.

The primary computation outcomes are provider-specific allocator processor
work and allocator-call count.  AGX and RP2040 work are never pooled inside a
primary policy-versus-Eager estimate.  Processor work is summed over calls and
logical robots and must not be interpreted as mission wall delay.

## Core semantics

- A logical allocation epoch is a scheduler admission/mandatory trigger.
- A call is one logical robot's `choose_goal` invocation.  Calls can continue
  after the epoch's expected first calls close it.
- `first_round=true` is the first valid call by each raw expected robot.  The
  hardware epoch format omits the expected set; initial/admission epochs infer
  four robots, while local mandatory epochs infer the first associated robot.
- `post_closure=true` means call completion is more than {TOLERANCE_S} s after
  the first-expected-call epoch close; no such call is discarded.
- Any-source assignment includes a physical-service fallback when a robot
  reaches an admitted task before an allocator owns it.  That source is
  identified when first assignment and completion have the same timestamp;
  allocator-only timestamps remain empty in those cases.
- Queue event statistics (one observation per admission epoch) remain separate
  from task-weighted release-to-admission waiting.
- Virtual compute-union fields are interval diagnostics, not a formal critical
  path or a direct attribution of mission delay.
- Raw movement schedules can retain in-flight edges completing after mission
  termination.  Step, trajectory, final-position, and semantic-event fields
  include only edges completed by the mission/horizon timestamp.
- Quantiles use Type-7 linear interpolation.  Seconds retain Python float
  round-trip precision.  Missing numerics are empty, never silently zero.
- Trace ID—not task, call, or epoch—is the environmental replication unit.  No
  task/call/epoch p-values or confidence intervals are included.

The bounded B=4/W=5 policy is checked against scheduler pending age at
admission.  Release-to-assignment or release-to-completion may exceed five
seconds without being a policy violation.

## Reproducibility

Exporter command:

```bash
{command}
```

The script discovers campaign roots from the existing publication source
labels and supports both `allocation_epochs.csv` and hardware
`reallocation_events.csv`.  It preserves retained algorithmic noncompletions,
excludes recovered historical technical attempts from scientific trial counts,
hashes all selected raw evidence, and reconciles the new reconstruction with
`publication/aug14_final_v1` at a {TOLERANCE_S} s numerical tolerance.

The prompt named `ARCHITECTURE_CLARIFICATION.md`; that filename is absent in
the repository.  The retained `docs/SIMULATION_ARCHITECTURE.md` in the frozen
worktree is the architecture semantic source and the discrepancy is reported
in the missing-data audit.

The optional full-call export is excluded by default.  `export_manifest.json`
contains sizes, row/column counts, and SHA-256 hashes for all bundle files
except the manifest's own mathematically self-referential hash.
"""


def _upload_priority_text() -> str:
    return """# Upload priority

## Priority 1 — always upload

- `README.md`
- `export_manifest.json`
- `data_dictionary.csv`
- `validation/BUILD_REPORT.md`
- `validation/validation_checks.csv`
- `core/trial_mechanism_summary.csv`
- `core/task_admission_reason_summary.csv`
- `core/call_epoch_trial_summary.csv`
- `core/policy_vs_eager_same_provider_extended.csv`
- `core/policy_pairwise_same_provider_extended.csv`
- `core/causal_zero_pairs_extended.csv`
- `core/cbaa_liveness_summary.csv`
- `core/cbaa_divergence_summary.csv`
- `core/hardware_call_complexity_summary.csv`

## Priority 2 — full report

- `detail/task_timing_decomposition_causal.csv.gz`
- `detail/task_timing_decomposition_zero.csv.gz`
- `detail/task_vs_eager_same_provider.csv.gz`
- all `detail/epoch_level_*.csv.gz` files
- `detail/hardware_call_level.csv.gz`
- `detail/representative_case_events.csv.gz`
- `detail/cbaa_divergence_events.csv.gz`

## Priority 3 — archival/reproducibility

- `core/raw_artifact_index.csv`
- `build_paper_v2_data_bundle.py`
- `requirements.txt`
- reconciliation and missing-data audits
- optional `detail/all_call_level_*_partNN.csv.gz` exports
"""


def _build_report_text(
    command: str,
    campaigns: Mapping[str, Path],
    commit: str,
    registry: CsvRegistry,
    validations: Sequence[Mapping[str, Any]],
    mismatches: Sequence[Mapping[str, Any]],
    include_full: bool,
    output: Path,
) -> str:
    failed = [row for row in validations if _truth(row["pass"]) is False or row["pass"] is False]
    total_size = sum(path.stat().st_size for path in output.rglob("*") if path.is_file())
    campaign_lines = "\n".join(f"- `{label}`: `{path}`" for label, path in sorted(campaigns.items()))
    count_lines = "\n".join(
        f"- `{name}`: {meta['row_count']:,} rows, {meta['column_count']} columns"
        for name, meta in sorted(registry.records.items())
    )
    missing_lines = "\n".join(f"- {area}: {fields} — {reason}" for area, fields, _, reason in MISSING_ROWS)
    return f"""# Paper-V2 bundle build report

- Git commit: `{commit}`
- Exporter command: `{command}`
- Optional full-call export: `{'created' if include_full else 'not created'}`
- Validation checks: {len(validations) - len(failed)} passed, {len(failed)} failed
- Reconciliation mismatches: {len(mismatches)}
- Current unpacked bundle size before final manifest: {total_size:,} bytes

## Raw roots and campaigns

{campaign_lines}

## Output counts

{count_lines}

## Missing/non-derivable evidence

{missing_lines}

## Scientific limitations

The raw traces do not retain a unified scheduler/message event log, complete
assignment-owner histories, cross-worker host-wall measurement timestamps, or
all robot diagnostic counters.  The exporter therefore labels reconstructed
semantic comparisons and virtual interval unions as diagnostics rather than a
formal critical-path proof.  Algorithmic noncompletions remain in denominators
and never receive an imputed mission elapsed time.

See `validation_checks.csv`, `publication_reconciliation.csv`,
`reconciliation_mismatches.csv`, and `missing_data_audit.csv` for machine-readable details.
"""


def _zip_deterministic(source: Path, destination: Path) -> None:
    with zipfile.ZipFile(destination, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=6) as archive:
        for path in sorted(item for item in source.rglob("*") if item.is_file()):
            relative = str(Path(source.name) / path.relative_to(source))
            info = zipfile.ZipInfo(relative, date_time=(1980, 1, 1, 0, 0, 0))
            info.compress_type = zipfile.ZIP_DEFLATED
            info.external_attr = (0o644 & 0xFFFF) << 16
            archive.writestr(info, path.read_bytes())


def build(
    repository_root: Path,
    raw_root: Path,
    publication_bundle: Path,
    output: Path,
    include_full_call_export: bool,
    argv: Sequence[str],
) -> dict[str, Any]:
    if output.exists():
        raise FileExistsError(f"refusing to overwrite existing output directory: {output}")
    zip_path = output.with_suffix(".zip")
    if zip_path.exists():
        raise FileExistsError(f"refusing to overwrite existing ZIP: {zip_path}")
    primary_path = publication_bundle / "data/primary_trial_level.csv"
    verification_path = publication_bundle / "data/verification_trial_level.csv"
    old_pairs_path = publication_bundle / "data/policy_vs_eager_paired_trial_level.csv"
    primary_publication, primary_fields = _read_csv(primary_path)
    verification_publication, verification_fields = _read_csv(verification_path)
    old_pairs, old_pair_fields = _read_csv(old_pairs_path)
    all_publication = [*primary_publication, *verification_publication]
    trials, campaigns = _discover_trials(all_publication, repository_root, raw_root)
    manifest_index = _manifest_hash_index(repository_root, raw_root)
    output.mkdir(parents=True)
    for directory in ("core", "detail", "validation"):
        (output / directory).mkdir()
    registry = CsvRegistry(output)
    command = " ".join(argv)
    commit = subprocess.run(["git", "rev-parse", "HEAD"], cwd=repository_root, check=True, capture_output=True, text=True).stdout.strip()
    _write_text(output / "README.md", _readme_text(command))
    _write_text(output / "UPLOAD_PRIORITY.md", _upload_priority_text())
    _write_text(output / "requirements.txt", "Python >= 3.10 (standard library only)")
    shutil.copy2(Path(__file__).resolve(), output / "build_paper_v2_data_bundle.py")

    representative_keys = {key for _, _, keys in _representative_specs() for key in keys}
    relevant_semantic = set(representative_keys)
    for publication in primary_publication:
        if publication["dataset"] == "causal" and publication["algorithm"] == "CBAA" and _truth(publication["all_tasks_completed"]) is False:
            key = _trial_key(publication)
            relevant_semantic.add(key)
            relevant_semantic.add(("zero_compute", key[1], key[2], key[3], key[4]))
            if key[3] == "eager_b1":
                for policy in ("count_b4", "bounded_b4_w5"):
                    relevant_semantic.add(("causal", "CBAA", key[2], policy, key[4]))

    raw_index_rows: list[dict[str, Any]] = []
    call_summary_rows: list[dict[str, Any]] = []
    admission_summary_rows: list[dict[str, Any]] = []
    primary_summary_rows: list[dict[str, Any]] = []
    verification_summary_rows: list[dict[str, Any]] = []
    summary_results: dict[tuple[str, str, str, str, str], dict[str, Any]] = {}
    coverage_source_rows: list[dict[str, Any]] = []
    hardware_complexity: dict[tuple[Any, ...], dict[str, list[float]]] = {}
    mechanism_audit: Counter[str] = Counter()
    task_pair_writer = GzipCsvWriter(registry, "detail/task_vs_eager_same_provider.csv.gz", TASK_PAIR_FIELDS)
    task_causal_writer = GzipCsvWriter(registry, "detail/task_timing_decomposition_causal.csv.gz", TASK_FIELDS)
    task_zero_writer = GzipCsvWriter(registry, "detail/task_timing_decomposition_zero.csv.gz", TASK_FIELDS)
    epoch_causal_writer = GzipCsvWriter(registry, "detail/epoch_level_causal_part01.csv.gz", EPOCH_FIELDS)
    epoch_zero_writer = GzipCsvWriter(registry, "detail/epoch_level_zero_part01.csv.gz", EPOCH_FIELDS)
    epoch_verification_writer = GzipCsvWriter(registry, "detail/epoch_level_verification.csv.gz", EPOCH_FIELDS)
    hardware_writer = GzipCsvWriter(registry, "detail/hardware_call_level.csv.gz", HARDWARE_CALL_FIELDS)
    full_writers: dict[str, GzipCsvWriter] = {}
    if include_full_call_export:
        full_writers = {
            "causal": GzipCsvWriter(registry, "detail/all_call_level_causal_part01.csv.gz", HARDWARE_CALL_FIELDS),
            "zero_compute": GzipCsvWriter(registry, "detail/all_call_level_zero_part01.csv.gz", HARDWARE_CALL_FIELDS),
        }

    # Process in groups that keep the five policy task tables together.  This
    # permits same-provider task pairing without retaining all 300,000 task
    # records in memory.
    grouped_keys: dict[tuple[str, str, str, str], list[tuple[str, str, str, str, str]]] = defaultdict(list)
    for key in trials:
        grouped_keys[(key[0], key[1], key[2], key[4])].append(key)
    for group_key in sorted(grouped_keys, key=lambda key: (
        ("causal", "zero_compute", "arrival_verification", "timeout_verification").index(key[0]),
        ALGORITHMS.index(key[1]), LOADS.index(key[2]), _trace_number(key[3]),
    )):
        group_results: dict[str, TrialResult] = {}
        for key in sorted(grouped_keys[group_key], key=lambda value: POLICIES.index(value[3])):
            trial = trials[key]
            if trial.dataset == "causal":
                task_writer, epoch_writer = task_causal_writer, epoch_causal_writer
            elif trial.dataset == "zero_compute":
                task_writer, epoch_writer = task_zero_writer, epoch_zero_writer
            else:
                task_writer, epoch_writer = None, epoch_verification_writer
            result, raw_index, calls_compact, admissions_compact = _process_trial(
                trial, repository_root, raw_root, manifest_index, task_writer, epoch_writer,
                hardware_writer, full_writers.get(trial.dataset), key in relevant_semantic,
                hardware_complexity,
            )
            raw_index_rows.append(raw_index)
            call_summary_rows.extend(calls_compact)
            admission_summary_rows.extend(admissions_compact)
            if trial.dataset in {"causal", "zero_compute"}:
                primary_summary_rows.append(result.row)
            else:
                # Explicit engineering observations remain separate.
                result.row.update({
                    "eager_arrival_trigger_observed": any(epoch["normalized_epoch_reason"] == "task_arrival_eager" for epoch in result.epoch_rows),
                    "count_b_threshold_trigger_observed": any(epoch["normalized_epoch_reason"] == "batch_threshold" for epoch in result.epoch_rows),
                    "bounded_timeout_trigger_observed": any(epoch["normalized_epoch_reason"] == "age_timeout" for epoch in result.epoch_rows),
                    "maximum_pending_age_at_timeout_s": max((epoch["oldest_pending_age_before_admission_s"] for epoch in result.epoch_rows if epoch["normalized_epoch_reason"] == "age_timeout"), default=None),
                    "w5_compliance": result.row["bounded_pending_age_violation_count"] == 0 if result.row["policy_id"] == "bounded_b4_w5" else None,
                    "parity_status": result.row.get("parity_passed"), "completion_status": result.row.get("all_tasks_completed"),
                })
                verification_summary_rows.append(result.row)
            for task in result.tasks:
                release = task["release_time_s"]
                admission = task["admission_time_s"]
                assignment = task["first_any_assignment_time_s"]
                completion = task["completion_time_s"]
                if admission is not None:
                    mechanism_audit["admission_order_checked"] += 1
                    mechanism_audit["admission_order_violations"] += admission + TOLERANCE_S < release
                if assignment is not None and admission is not None:
                    mechanism_audit["assignment_identity_checked"] += 1
                    left = task["release_to_first_any_assignment_latency_s"]
                    right = task["release_to_admission_latency_s"] + task["admission_to_first_any_assignment_latency_s"]
                    mechanism_audit["assignment_identity_violations"] += abs(left - right) > TOLERANCE_S
                    mechanism_audit["assignment_order_violations"] += assignment + TOLERANCE_S < admission
                if completion is not None and assignment is not None and admission is not None:
                    mechanism_audit["service_identity_checked"] += 1
                    left = task["release_to_completion_latency_s"]
                    right = task["release_to_admission_latency_s"] + task["admission_to_first_any_assignment_latency_s"] + task["first_any_assignment_to_completion_latency_s"]
                    mechanism_audit["service_identity_violations"] += abs(left - right) > TOLERANCE_S
                    mechanism_audit["completion_order_violations"] += completion + TOLERANCE_S < assignment
                if task["first_assignment_source"] == "physical_service_fallback":
                    mechanism_audit["fallback_allocator_only_defined_violations"] += task["first_allocator_assignment_time_s"] is not None
            mechanism_audit["call_count_mismatches"] += int(result.row["total_valid_allocator_call_count"] != _integer(trial.summary.get("allocator_call_count")))
            mechanism_audit["epoch_count_mismatches"] += int(result.row["total_epoch_count"] != _integer(trial.summary.get("allocation_epoch_count")))
            mechanism_audit["first_round_excess_epochs"] += sum(
                _integer(epoch["observed_first_round_robot_count"]) > _integer(epoch["expected_first_round_robot_count"])
                for epoch in result.epoch_rows
            )
            compact = {
                "row": result.row,
                "movement_signature_sha256": result.movement_signature_sha256,
                "assignment_signature_sha256": result.assignment_signature_sha256,
                "completion_signature_sha256": result.completion_signature_sha256,
                "raw_summary": trial.summary,
                "raw_index": raw_index,
            }
            if key in relevant_semantic:
                compact["semantic_events"] = result.semantic_events
                compact["tasks"] = result.tasks
            summary_results[key] = compact
            group_results[result.row["policy_id"]] = result
            coverage_source_rows.append({
                **result.row, "raw_call_file_complete": raw_index["call_artifact_complete"],
                "raw_epoch_file_complete": raw_index["epoch_artifact_complete"],
                "raw_task_file_complete": raw_index["task_artifact_complete"],
                "raw_movement_file_complete": raw_index["movement_artifact_complete"],
            })
        eager = group_results.get("eager_b1")
        if eager is not None and group_key[0] in {"causal", "zero_compute"}:
            for policy, result in group_results.items():
                if policy != "eager_b1" and result.row["provider"] == eager.row["provider"]:
                    for pair_row in _task_pair_rows(result, eager):
                        task_pair_writer.writerow(pair_row)
        # Remove bulky task/call objects before the next trace group.
        group_results.clear()

    for writer in (task_pair_writer, task_causal_writer, task_zero_writer, epoch_causal_writer, epoch_zero_writer, epoch_verification_writer, hardware_writer, *full_writers.values()):
        writer.close()

    primary_summary_rows.sort(key=_sort_key)
    verification_summary_rows.sort(key=_sort_key)
    raw_index_rows.sort(key=_sort_key)
    call_summary_rows.sort(key=_sort_key)
    admission_summary_rows.sort(key=_sort_key)
    registry.write("core/raw_artifact_index.csv", raw_index_rows, RAW_INDEX_FIELDS)
    matrix_rows = _matrix_coverage_rows(coverage_source_rows)
    matrix_fields = list(matrix_rows[0])
    registry.write("core/matrix_coverage.csv", matrix_rows, matrix_fields)
    registry.write("core/trial_mechanism_summary.csv", primary_summary_rows, [*primary_fields, *TRIAL_APPEND_FIELDS])
    verification_extra = ["eager_arrival_trigger_observed", "count_b_threshold_trigger_observed", "bounded_timeout_trigger_observed", "maximum_pending_age_at_timeout_s", "w5_compliance", "parity_status", "completion_status"]
    registry.write("core/verification_trial_mechanism_summary.csv", verification_summary_rows, [*verification_fields, *TRIAL_APPEND_FIELDS, *verification_extra])
    registry.write("core/task_admission_reason_summary.csv", admission_summary_rows, ADMISSION_SUMMARY_FIELDS)
    registry.write("core/call_epoch_trial_summary.csv", call_summary_rows, CALL_SUMMARY_FIELDS)

    # Policy pairs, preserving every existing audit comparison.
    old_pair_by_key = {
        (row["dataset"], row["algorithm"], row["arrival_load"], row["policy_id"], row["trace_id"]): row
        for row in old_pairs
    }
    all_audit = []
    same_provider = []
    policy_pairwise = []
    for dataset in ("causal", "zero_compute"):
        for algorithm in ALGORITHMS:
            for load in LOADS:
                for trace in range(50):
                    trace_id = f"trace_{trace:04d}"
                    eager = summary_results[(dataset, algorithm, load, "eager_b1", trace_id)]["row"]
                    policies = [summary_results[(dataset, algorithm, load, policy, trace_id)]["row"] for policy in POLICIES]
                    for policy in policies[1:]:
                        existing = old_pair_by_key.get((dataset, algorithm, load, policy["policy_id"], trace_id))
                        pair = _policy_eager_pair(policy, eager, existing)
                        all_audit.append(pair)
                        if pair["usable_for_primary_policy_effect"]:
                            same_provider.append(pair)
                    for reference_index, comparison_index in itertools.combinations(range(len(policies)), 2):
                        reference, comparison = policies[reference_index], policies[comparison_index]
                        if reference["provider"] == comparison["provider"]:
                            policy_pairwise.append(_policy_pairwise(reference, comparison))
    audit_fields = list(dict.fromkeys([*old_pair_fields, *all_audit[0].keys()]))
    extended_fields = list(same_provider[0])
    pairwise_fields = list(policy_pairwise[0])
    registry.write("core/policy_vs_eager_all_pairs_audit.csv", all_audit, audit_fields)
    registry.write("core/policy_vs_eager_same_provider_extended.csv", same_provider, extended_fields)
    registry.write("core/policy_pairwise_same_provider_extended.csv", policy_pairwise, pairwise_fields)

    causal_zero = []
    for algorithm in ALGORITHMS:
        for load in LOADS:
            for policy in POLICIES:
                for trace in range(50):
                    trace_id = f"trace_{trace:04d}"
                    causal_zero.append(_causal_zero_pair(
                        summary_results[("causal", algorithm, load, policy, trace_id)],
                        summary_results[("zero_compute", algorithm, load, policy, trace_id)],
                    ))
    registry.write("core/causal_zero_pairs_extended.csv", causal_zero, list(causal_zero[0]))
    cbaa_liveness = _cbaa_liveness_rows(summary_results)
    registry.write("core/cbaa_liveness_summary.csv", cbaa_liveness, CBAA_LIVENESS_FIELDS)

    divergences = []
    for key, compact in summary_results.items():
        row = compact["row"]
        if key[0] != "causal" or key[1] != "CBAA" or _truth(row.get("all_tasks_completed")) is not False:
            continue
        zero = summary_results.get(("zero_compute", key[1], key[2], key[3], key[4]))
        if zero:
            divergences.append(_divergence_compare(f"CZ__{key[2]}__{key[3]}__{key[4]}", "causal_failed_vs_exact_zero", zero, compact))
        if key[3] == "eager_b1":
            for policy in ("count_b4", "bounded_b4_w5"):
                coalesced = summary_results.get(("causal", "CBAA", key[2], policy, key[4]))
                if coalesced and _truth(coalesced["row"].get("all_tasks_completed")) is True:
                    divergences.append(_divergence_compare(f"PC__{key[2]}__{policy}__{key[4]}", "failed_eager_vs_completed_coalesced", coalesced, compact))
    divergence_export = [{field: row.get(field) for field in DIVERGENCE_FIELDS} for row in divergences]
    registry.write("core/cbaa_divergence_summary.csv", divergence_export, DIVERGENCE_FIELDS)
    with GzipCsvWriter(registry, "detail/cbaa_divergence_events.csv.gz", DIVERGENCE_EVENT_FIELDS) as writer:
        for row in _divergence_event_rows(divergences):
            writer.writerow(row)
    with GzipCsvWriter(registry, "detail/representative_case_events.csv.gz", REP_EVENT_FIELDS) as writer:
        for row in _representative_rows(summary_results):
            writer.writerow(row)

    hardware_complexity_rows = _hardware_complexity_rows(hardware_complexity)
    registry.write("core/hardware_call_complexity_summary.csv", hardware_complexity_rows, HARDWARE_COMPLEXITY_FIELDS)
    hardware_env = _hardware_environment_rows(repository_root, campaigns, raw_index_rows)
    registry.write("core/hardware_environment.csv", hardware_env, HARDWARE_ENV_FIELDS)

    # Reconciliation against every metric/status/hash represented in the old
    # primary trial export.  Derived values take precedence; raw summary values
    # cover diagnostic fields that have no lower-level event reconstruction.
    mismatch_rows = []
    reconciliation_by_metric: dict[str, dict[str, Any]] = defaultdict(lambda: {"compared": 0, "missing_old": 0, "missing_new": 0, "mismatch": 0, "max_abs": 0.0})
    provenance_columns = set(primary_fields[:29]) | {"historical_validation_message"}
    for publication in primary_publication:
        compact = summary_results[_trial_key(publication)]
        # Use equivalent appended values reconstructed from task/epoch/call
        # streams; diagnostic fields without a lower-level representation are
        # checked directly against the retained raw trial summary below.
        reconstructed: dict[str, Any] = {}
        appended_map = {
            "allocator_call_count": compact["row"]["total_valid_allocator_call_count"],
            "allocation_epoch_count": compact["row"]["total_epoch_count"],
            "mission_elapsed_time_s": compact["row"].get("mission_elapsed_time_s"),
            "mean_release_to_first_assignment_latency_s": compact["row"]["all_release_to_first_any_assignment_mean_s"],
            "median_release_to_first_assignment_latency_s": compact["row"]["all_release_to_first_any_assignment_median_s"],
            "max_release_to_first_assignment_latency_s": compact["row"]["all_release_to_first_any_assignment_max_s"],
            "mean_release_to_completion_latency_s": compact["row"]["all_release_to_completion_mean_s"],
            "median_release_to_completion_latency_s": compact["row"]["all_release_to_completion_median_s"],
            "max_release_to_completion_latency_s": compact["row"]["all_release_to_completion_max_s"],
            "max_robot_steps": compact["row"]["reconstructed_max_robot_steps"],
            "total_team_steps": compact["row"]["reconstructed_total_team_steps"],
            "agx_allocator_processor_work_s": compact["row"]["total_agx_allocator_work_s"],
            "rp2040_allocator_processor_work_s": compact["row"]["total_rp2040_allocator_work_s"],
            "cumulative_allocator_time_s": compact["row"]["injected_provider_work_s"],
            "all_tasks_completed": _truth(compact["row"].get("all_tasks_completed")),
            "algorithmic_failure_type": compact["row"].get("algorithmic_failure_type"),
            "task_events_sha256": compact["raw_index"]["task_event_sha256"],
            "allocation_epochs_sha256": compact["raw_index"]["epoch_artifact_sha256"],
        }
        reconstructed.update(appended_map)
        for metric in primary_fields:
            if metric in provenance_columns:
                continue
            old = publication.get(metric)
            new = reconstructed.get(metric, compact["raw_summary"].get(metric))
            stats_row = reconciliation_by_metric[metric]
            if old in {None, ""}:
                stats_row["missing_old"] += 1
                continue
            if new in {None, ""}:
                stats_row["missing_new"] += 1
                mismatch = True
                difference = None
            else:
                stats_row["compared"] += 1
                old_number, new_number = _number(old), _number(new)
                if old_number is not None and new_number is not None:
                    difference = abs(old_number - new_number)
                    tolerance = TOLERANCE_S + TOLERANCE_S * max(abs(old_number), abs(new_number))
                    mismatch = difference > tolerance
                    stats_row["max_abs"] = max(stats_row["max_abs"], difference)
                elif _truth(old) is not None and _truth(new) is not None:
                    difference = None
                    mismatch = _truth(old) != _truth(new)
                else:
                    difference = None
                    mismatch = str(old) != str(new or "")
            if mismatch:
                stats_row["mismatch"] += 1
                mismatch_rows.append({
                    **{field: publication.get(field) for field in ("dataset", "provider", "algorithm", "arrival_load", "policy_id", "trace_id", "job_id")},
                    "metric": metric, "old_value": old, "new_value": new, "absolute_difference": difference,
                    "likely_cause": "raw reconstruction differs from compact publication value; inspect source artifact and tolerance",
                })
    reconciliation_rows = [
        {
            "metric": metric, "compared_trial_count": values["compared"], "old_missing_count": values["missing_old"],
            "new_missing_count": values["missing_new"], "mismatch_count": values["mismatch"],
            "maximum_absolute_difference": values["max_abs"], "numerical_tolerance_s": TOLERANCE_S,
            "pass": values["mismatch"] == 0,
        }
        for metric, values in sorted(reconciliation_by_metric.items())
    ]
    registry.write("validation/publication_reconciliation.csv", reconciliation_rows, list(reconciliation_rows[0]))
    mismatch_fields = ["dataset", "provider", "algorithm", "arrival_load", "policy_id", "trace_id", "job_id", "metric", "old_value", "new_value", "absolute_difference", "likely_cause"]
    registry.write("validation/reconciliation_mismatches.csv", mismatch_rows, mismatch_fields)
    missing_rows = _missing_audit_rows()
    registry.write("validation/missing_data_audit.csv", missing_rows, list(missing_rows[0]))

    causal_rows = [row for row in primary_summary_rows if row["dataset"] == "causal"]
    zero_rows = [row for row in primary_summary_rows if row["dataset"] == "zero_compute"]
    validations = [
        _validation_row("matrix.causal_trials", "error", 3000, len(causal_rows), len(causal_rows) == 3000),
        _validation_row("matrix.zero_trials", "error", 3000, len(zero_rows), len(zero_rows) == 3000),
        _validation_row("matrix.hardware_trials", "error", 104, sum(row["provider"] == "rp2040_hardware" for row in causal_rows), sum(row["provider"] == "rp2040_hardware" for row in causal_rows) == 104),
        _validation_row("matrix.verification_trials", "error", 144, len(verification_summary_rows), len(verification_summary_rows) == 144),
        _validation_row("matrix.agx_causal_trials", "error", 2896, sum(row["provider"] == "agx_host_proxy" for row in causal_rows), sum(row["provider"] == "agx_host_proxy" for row in causal_rows) == 2896),
        _validation_row("matrix.hardware_core_trials", "error", 96, sum(row["source_campaign"] == "hardware_core" for row in causal_rows), sum(row["source_campaign"] == "hardware_core" for row in causal_rows) == 96),
        _validation_row("matrix.hardware_bounded_trials", "error", 8, sum(row["source_campaign"] == "hardware_bounded" for row in causal_rows), sum(row["source_campaign"] == "hardware_bounded" for row in causal_rows) == 8),
        _validation_row("matrix.hardware_core_unique_traces", "error", 4, len({row["trace_id"] for row in causal_rows if row["source_campaign"] == "hardware_core"}), len({row["trace_id"] for row in causal_rows if row["source_campaign"] == "hardware_core"}) == 4, "104 hardware rows are not 104 independent scenarios"),
        _validation_row("matrix.no_duplicate_primary_keys", "error", 6000, len({_trial_key(row) for row in primary_summary_rows}), len({_trial_key(row) for row in primary_summary_rows}) == 6000),
        _validation_row("detail.causal_task_rows", "error", 150000, registry.records["detail/task_timing_decomposition_causal.csv.gz"]["row_count"], registry.records["detail/task_timing_decomposition_causal.csv.gz"]["row_count"] == 150000),
        _validation_row("detail.zero_task_rows", "error", 150000, registry.records["detail/task_timing_decomposition_zero.csv.gz"]["row_count"], registry.records["detail/task_timing_decomposition_zero.csv.gz"]["row_count"] == 150000),
        _validation_row("detail.task_same_provider_pair_rows", "error", 233200, registry.records["detail/task_vs_eager_same_provider.csv.gz"]["row_count"], registry.records["detail/task_vs_eager_same_provider.csv.gz"]["row_count"] == 233200),
        _validation_row("pairs.all_eager", "error", 4800, len(all_audit), len(all_audit) == 4800),
        _validation_row("pairs.same_provider_eager", "error", 4664, len(same_provider), len(same_provider) == 4664),
        _validation_row("pairs.no_cross_provider_primary", "error", 0, sum(row["provider_match_type"] == "cross_provider" for row in same_provider), all(row["provider_match_type"] != "cross_provider" for row in same_provider)),
        _validation_row("pairs.causal_zero", "error", 3000, len(causal_zero), len(causal_zero) == 3000),
        _validation_row("raw.all_complete", "error", len(raw_index_rows), sum(row["all_required_raw_artifacts_complete"] for row in raw_index_rows), all(row["all_required_raw_artifacts_complete"] for row in raw_index_rows)),
        _validation_row("bounded.pending_age", "error", 0, sum(row["bounded_pending_age_violation_count"] or 0 for row in primary_summary_rows if row["policy_id"] == "bounded_b4_w5" and _truth(row["all_tasks_completed"]) is True), sum(row["bounded_pending_age_violation_count"] or 0 for row in primary_summary_rows if row["policy_id"] == "bounded_b4_w5" and _truth(row["all_tasks_completed"]) is True) == 0, "release-to-assignment above 5 s is not a violation"),
        _validation_row("task.assignment_decomposition", "error", 0, mechanism_audit["assignment_identity_violations"], mechanism_audit["assignment_identity_violations"] == 0, f"checked {mechanism_audit['assignment_identity_checked']} tasks"),
        _validation_row("task.service_decomposition", "error", 0, mechanism_audit["service_identity_violations"], mechanism_audit["service_identity_violations"] == 0, f"checked {mechanism_audit['service_identity_checked']} tasks including fallback any-source paths"),
        _validation_row("task.timestamp_order", "error", 0, mechanism_audit["admission_order_violations"] + mechanism_audit["assignment_order_violations"] + mechanism_audit["completion_order_violations"], mechanism_audit["admission_order_violations"] + mechanism_audit["assignment_order_violations"] + mechanism_audit["completion_order_violations"] == 0),
        _validation_row("task.fallback_allocator_only_missing", "error", 0, mechanism_audit["fallback_allocator_only_defined_violations"], mechanism_audit["fallback_allocator_only_defined_violations"] == 0),
        _validation_row("epoch.raw_count_identity", "error", 0, mechanism_audit["epoch_count_mismatches"], mechanism_audit["epoch_count_mismatches"] == 0),
        _validation_row("call.raw_valid_count_identity", "error", 0, mechanism_audit["call_count_mismatches"], mechanism_audit["call_count_mismatches"] == 0),
        _validation_row("call.first_round_expectation", "error", 0, mechanism_audit["first_round_excess_epochs"], mechanism_audit["first_round_excess_epochs"] == 0),
        _validation_row("reconciliation.mismatches", "error", 0, len(mismatch_rows), len(mismatch_rows) == 0),
        _validation_row("noncompletion.mission_blank", "error", 0, sum(bool(row.get("mission_elapsed_time_s")) for row in primary_summary_rows if _truth(row["all_tasks_completed"]) is False), all(not row.get("mission_elapsed_time_s") for row in primary_summary_rows if _truth(row["all_tasks_completed"]) is False)),
    ]
    # Explicit expected same-provider causal counts.
    expected_policy_provider = {
        ("count_b2", "same_agx"): 552, ("count_b2", "same_rp2040"): 0,
        ("count_b4", "same_agx"): 552, ("count_b4", "same_rp2040"): 48,
        ("count_b8", "same_agx"): 552, ("count_b8", "same_rp2040"): 0,
        ("bounded_b4_w5", "same_agx"): 552, ("bounded_b4_w5", "same_rp2040"): 8,
    }
    for pair_key, expected in expected_policy_provider.items():
        observed = sum(row["dataset"] == "causal" and row["policy_id"] == pair_key[0] and row["provider_match_type"] == pair_key[1] for row in same_provider)
        validations.append(_validation_row(f"pairs.causal.{pair_key[0]}.{pair_key[1]}", "error", expected, observed, observed == expected))
    registry.write("validation/validation_checks.csv", validations, ["check_id", "severity", "expected_value", "observed_value", "pass", "details"])

    _write_text(output / "validation/BUILD_REPORT.md", _build_report_text(
        command, campaigns, commit, registry, validations, mismatch_rows,
        include_full_call_export, output,
    ))

    # Data dictionary: one row for every column of every tabular output,
    # including the dictionary itself.
    dictionary_fields = [
        "file_name", "column_name", "data_type", "units", "clock_source", "precise_formula",
        "accumulation_scope", "inclusion_exclusion_rules", "missing_value_interpretation",
        "source_raw_artifact", "source_repository_file_and_line_reference", "notes",
    ]
    dictionary_rows = []
    for file_name, meta in sorted(registry.records.items()):
        for column in meta["columns"]:
            dictionary_rows.append(_dictionary_metadata(file_name, column))
    for column in dictionary_fields:
        dictionary_rows.append(_dictionary_metadata("data_dictionary.csv", column))
    registry.write("data_dictionary.csv", dictionary_rows, dictionary_fields)

    # Record non-tabular bundle files and finalize hashes.  The manifest cannot
    # contain its own SHA-256 without an infinite self-reference.
    files = sorted(path for path in output.rglob("*") if path.is_file() and path.name != "export_manifest.json")
    manifest_files = {}
    for path in files:
        relative = str(path.relative_to(output))
        tabular = registry.records.get(relative, {})
        manifest_files[relative] = {
            "row_count": tabular.get("row_count"), "column_count": tabular.get("column_count"),
            "byte_size": path.stat().st_size, "sha256": _sha256(path),
        }
    manifest = {
        "schema_version": 2, "report_kind": "paper_v2_latency_mechanism_data_bundle",
        "generated_at": dt.datetime.now(dt.timezone.utc).isoformat().replace("+00:00", "Z"),
        "git_commit": commit, "exporter_command": command, "repository_root": str(repository_root),
        "raw_campaign_root": str(raw_root), "publication_bundle": str(publication_bundle),
        "output_directory": str(output), "optional_full_call_export_created": include_full_call_export,
        "numerical_tolerance_s": TOLERANCE_S, "quantile_definition": "Type-7 linear interpolation",
        "manifest_self_hash": None, "manifest_self_hash_note": "omitted because a file cannot contain its own stable SHA-256",
        "matrix": {
            "causal_trials": len(causal_rows), "zero_compute_trials": len(zero_rows),
            "verification_trials": len(verification_summary_rows), "hardware_backed_causal_trials": 104,
            "primary_task_rows": registry.records["detail/task_timing_decomposition_causal.csv.gz"]["row_count"] + registry.records["detail/task_timing_decomposition_zero.csv.gz"]["row_count"],
            "all_provider_eager_pairs": len(all_audit), "same_provider_eager_pairs": len(same_provider),
        },
        "validation": {"passed": sum(row["pass"] is True for row in validations), "failed": sum(row["pass"] is False for row in validations)},
        "reconciliation_mismatch_count": len(mismatch_rows), "files": manifest_files,
    }
    _write_text(output / "export_manifest.json", json.dumps(manifest, indent=2, sort_keys=True))
    _zip_deterministic(output, zip_path)
    manifest["zip_path"] = str(zip_path)
    manifest["zip_byte_size"] = zip_path.stat().st_size
    manifest["zip_sha256"] = _sha256(zip_path)
    return manifest


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repository-root", type=Path, required=True)
    parser.add_argument("--raw-campaign-root", type=Path, required=True)
    parser.add_argument("--publication-bundle", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--include-full-call-export", action="store_true")
    args = parser.parse_args()
    manifest = build(
        args.repository_root.resolve(), args.raw_campaign_root.resolve(),
        args.publication_bundle.resolve(), args.output_dir.resolve(),
        args.include_full_call_export, [sys.executable, str(Path(__file__).resolve()), *sys.argv[1:]],
    )
    print(json.dumps({
        "output_directory": manifest["output_directory"], "zip_path": manifest["zip_path"],
        "validation": manifest["validation"], "reconciliation_mismatch_count": manifest["reconciliation_mismatch_count"],
    }, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
